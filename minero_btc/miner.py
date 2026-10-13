"""Coordinador: conecta con el pool, reparte trabajo entre los procesos,
envía las shares y muestra estadísticas."""

from __future__ import annotations

import itertools
import multiprocessing
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from . import stats
from .consensus import (
    BLOCK_SUBSIDY_SATS,
    SATS_PER_BTC,
    bits_to_target,
    build_coinbase,
    coinbase_payout,
    difficulty_to_target,
    target_to_difficulty,
)
from .stratum import StratumClient, StratumError, format_error
from .worker import worker_main

# Dificultad de la red aproximada (octubre de 2026). Solo se usa en la prueba
# de velocidad sin conexión; minando se usa la dificultad real de cada trabajo.
REFERENCE_NETWORK_DIFFICULTY = 132.7e12
MAX_VALID_JOBS = 16


def log_line(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def btc(sats: int) -> str:
    return f"{sats / SATS_PER_BTC:.8f} BTC"


@dataclass
class MinerConfig:
    host: str
    port: int
    use_tls: bool
    address: str
    script_pubkey: bytes
    worker_name: str = "minero"
    password: str = "x"
    threads: int = field(default_factory=lambda: os.cpu_count() or 1)
    suggest_difficulty: float | None = 1
    stats_interval: float = 30.0
    low_priority: bool = True

    @property
    def username(self) -> str:
        return f"{self.address}.{self.worker_name}" if self.worker_name else self.address


class WorkerPool:
    """Procesos de minado (uno por hilo de CPU)."""

    def __init__(self, threads: int, low_priority: bool):
        # "spawn" es lo que usa Windows; lo usamos en todas partes para que se
        # comporte igual en cualquier sistema.
        ctx = multiprocessing.get_context("spawn")
        self.threads = threads
        self.results = ctx.Queue()
        self.hash_counter = ctx.Value("Q", 0)
        self._stop = ctx.Event()
        self._queues = [ctx.Queue() for _ in range(threads)]
        self._processes = [
            ctx.Process(
                target=worker_main,
                args=(i, threads, self._queues[i], self.results, self.hash_counter,
                      self._stop, low_priority),
                name=f"minero-{i}",
                daemon=True,
            )
            for i in range(threads)
        ]

    def start(self) -> None:
        for process in self._processes:
            process.start()

    def dispatch(self, job) -> None:
        for job_queue in self._queues:
            job_queue.put(job)

    def pause(self) -> None:
        self.dispatch("pause")

    def total_hashes(self) -> int:
        with self.hash_counter.get_lock():
            return self.hash_counter.value

    def alive(self) -> int:
        return sum(process.is_alive() for process in self._processes)

    def stop(self) -> None:
        self._stop.set()
        for job_queue in self._queues:
            try:
                job_queue.put("stop")
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + 5
        for process in self._processes:
            process.join(timeout=max(0.1, deadline - time.monotonic()))
        for process in self._processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        for job_queue in [*self._queues, self.results]:
            job_queue.cancel_join_thread()
            job_queue.close()


class HashrateMeter:
    def __init__(self, pool: WorkerPool):
        self._pool = pool
        self._last_hashes = pool.total_hashes()
        self._last_time = time.monotonic()
        self.hashrate = 0.0

    def update(self) -> float:
        now = time.monotonic()
        hashes = self._pool.total_hashes()
        elapsed = now - self._last_time
        if elapsed > 0:
            self.hashrate = (hashes - self._last_hashes) / elapsed
        self._last_hashes, self._last_time = hashes, now
        return self.hashrate


class Miner:
    reconnect_delay = 5.0  # segundos; se duplica en cada fallo seguido (máx. 2 min)

    def __init__(self, config: MinerConfig, log=log_line, stop_event: threading.Event | None = None):
        self.config = config
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.pool: WorkerPool | None = None
        self.accepted = 0
        self.rejected = 0
        self.stale = 0
        self.blocks_found = 0
        self.best_difficulty = 0.0
        self.network_difficulty: float | None = None
        self._payout_state: str | None = None
        self._handshake_ok = False
        self._seq = itertools.count(1)
        self._jobs: dict[int, dict] = {}
        self._pending: dict[int, dict] = {}

    # --- bucle principal -------------------------------------------------------

    def run(self) -> None:
        cfg = self.config
        self.log(f"Iniciando {cfg.threads} procesos de minado...")
        self.pool = WorkerPool(cfg.threads, cfg.low_priority)
        self.pool.start()
        self._meter = HashrateMeter(self.pool)
        self._next_stats = time.monotonic() + cfg.stats_interval
        backoff = self.reconnect_delay
        try:
            while not self.stop_event.is_set():
                self._handshake_ok = False
                reconnect_wait = None
                try:
                    reconnect_wait = self._session()
                except (OSError, StratumError) as exc:
                    self.log(f"Problema con el pool: {exc}")
                if self.stop_event.is_set():
                    break
                self.pool.pause()
                connected = self._handshake_ok
                if connected:
                    backoff = self.reconnect_delay
                wait = reconnect_wait if reconnect_wait is not None else backoff
                self.log(f"Reconectando en {wait:.0f} s...")
                self.stop_event.wait(wait)
                if not connected:
                    backoff = min(backoff * 2, 120.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.log("Deteniendo los procesos de minado...")
            self.pool.stop()
            self._print_stats(final=True)

    def _session(self) -> float | None:
        """Una conexión con el pool. Devuelve la espera pedida por el pool para reconectar."""
        cfg = self.config
        self.log(f"Conectando a {cfg.host}:{cfg.port}{' (TLS)' if cfg.use_tls else ''}...")
        client = StratumClient(cfg.host, cfg.port, cfg.use_tls)
        client.connect()
        self._jobs.clear()
        self._pending.clear()
        try:
            client.handshake(cfg.username, cfg.password, cfg.suggest_difficulty)
            self._handshake_ok = True
            self.log(f"Conectado. Minando para {cfg.address} (trabajador '{cfg.worker_name}').")
            self.difficulty = 1.0
            self.extranonce1 = client.extranonce1
            self.extranonce2_size = client.extranonce2_size
            while not self.stop_event.is_set():
                try:
                    event = client.events.get(timeout=0.2)
                except queue.Empty:
                    event = None
                if event is not None:
                    outcome = self._handle_event(client, event)
                    if outcome is not None:
                        return outcome
                self._drain_results(client)
                if time.monotonic() >= self._next_stats:
                    self._print_stats()
                    self._next_stats = time.monotonic() + cfg.stats_interval
            return None
        finally:
            client.close()

    # --- eventos del pool -------------------------------------------------------

    def _handle_event(self, client: StratumClient, event) -> float | None:
        """Procesa un evento. Devuelve segundos de espera si hay que reconectar."""
        kind = event[0]
        if kind == "notify":
            self._on_notify(event[1])
        elif kind == "set_difficulty":
            try:
                value = float(event[1])
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                if value != self.difficulty:
                    self.log(f"El pool fija la dificultad de share en {stats.format_difficulty(value)}.")
                self.difficulty = value  # se aplica a partir del siguiente trabajo
        elif kind == "set_extranonce":
            params = event[1]
            try:
                client._set_extranonce(params[0], params[1])
            except (IndexError, ValueError, StratumError) as exc:
                self.log(f"set_extranonce no válido: {exc}")
            else:
                self.extranonce1 = client.extranonce1
                self.extranonce2_size = client.extranonce2_size
        elif kind == "response":
            self._on_response(event[1], event[2], event[3])
        elif kind == "message":
            self.log(f"Mensaje del pool: {event[1]}")
        elif kind == "reconnect":
            params = event[1] or []
            wait = 5.0
            if len(params) >= 3:
                try:
                    wait = min(max(float(params[2]), 1.0), 300.0)
                except (TypeError, ValueError):
                    pass
            self.log("El pool pide reconectar.")
            return wait
        elif kind == "disconnected":
            raise ConnectionError(event[1])
        return None

    def _on_notify(self, params) -> None:
        try:
            (job_id, prevhash, coinb1, coinb2, branches,
             version, nbits, ntime, clean) = params[:9]
            job = {
                "seq": next(self._seq),
                "job_id": str(job_id),
                "prevhash": prevhash,
                "coinb1": bytes.fromhex(coinb1),
                "coinb2": bytes.fromhex(coinb2),
                "merkle_branch": [bytes.fromhex(b) for b in branches],
                "version": version,
                "nbits": nbits,
                "ntime": ntime,
                "extranonce1": self.extranonce1,
                "extranonce2_size": self.extranonce2_size,
                "share_target": difficulty_to_target(self.difficulty),
                "network_target": bits_to_target(int(nbits, 16)),
            }
            for value in (prevhash, version, nbits, ntime):
                bytes.fromhex(value)
            if len(prevhash) != 64 or not all(len(v) == 8 for v in (version, nbits, ntime)):
                raise ValueError("campos de longitud incorrecta")
        except (TypeError, ValueError) as exc:
            self.log(f"Trabajo no válido recibido del pool, se ignora ({exc}).")
            return
        if clean:
            self._jobs.clear()
        self._jobs[job["seq"]] = job
        while len(self._jobs) > MAX_VALID_JOBS:
            self._jobs.pop(next(iter(self._jobs)))
        difficulty = target_to_difficulty(job["network_target"])
        if difficulty != self.network_difficulty:
            self.log(f"Dificultad de la red: {stats.format_difficulty(difficulty)} "
                     "(un bloque necesita un hash al menos así de difícil).")
        self.network_difficulty = difficulty
        self._check_payout(job)
        self.pool.dispatch(job)

    def _check_payout(self, job) -> None:
        """Comprueba que la coinbase del pool paga la recompensa a tu dirección."""
        coinbase = build_coinbase(job["coinb1"], job["extranonce1"],
                                  b"\x00" * job["extranonce2_size"], job["coinb2"])
        try:
            mine, total = coinbase_payout(coinbase, self.config.script_pubkey)
        except (ValueError, KeyError, IndexError):
            mine, total = None, None
        state = "unreadable" if mine is None else ("mine" if mine > 0 else "not_mine")
        if state == self._payout_state:
            return
        self._payout_state = state
        if state == "unreadable":
            self.log("No se pudo leer la coinbase del pool para comprobar a quién paga.")
        elif state == "mine":
            share = mine / total * 100 if total else 0
            self.log(f"Comprobado: si encuentras este bloque, {btc(mine)} ({share:.1f} % de la "
                     f"recompensa) van DIRECTAMENTE a tu dirección {self.config.address}.")
            if share < 99.9:
                self.log("El resto de la recompensa es la comisión del pool.")
        else:
            self.log("ATENCIÓN: el bloque que prepara este pool NO paga a tu dirección. Este pool no "
                     "es de minería en solitario (o algo va mal). Usa public-pool.io:21496 o "
                     "solo.ckpool.org:3333 si quieres que la recompensa llegue directa a tu wallet.")

    # --- resultados de los procesos ----------------------------------------------

    def _drain_results(self, client: StratumClient) -> None:
        while True:
            try:
                result = self.pool.results.get_nowait()
            except queue.Empty:
                return
            self.best_difficulty = max(self.best_difficulty, result["difficulty"])
            if not result["is_share"]:
                continue
            job = self._jobs.get(result["seq"])
            if job is None:
                self.stale += 1
                if result["is_block"]:
                    self.log(f"Se encontró un bloque para un trabajo ya caducado ({result['hash']}); "
                             "otro minero se adelantó.")
                continue
            if result["is_block"]:
                self.blocks_found += 1
                self.log("*" * 70)
                self.log(f"¡¡¡BLOQUE ENCONTRADO!!! Hash {result['hash']}")
                self.log("Enviándolo al pool...")
                self.log("*" * 70)
            request_id = client.submit(self.config.username, job["job_id"], result["extranonce2"],
                                       result["ntime"], result["nonce"])
            self._pending[request_id] = result

    def _on_response(self, request_id, result, error) -> None:
        share = self._pending.pop(request_id, None)
        if share is None:
            return  # respuesta a otra petición (p. ej. suggest_difficulty)
        if result is True:
            self.accepted += 1
            if share["is_block"]:
                self.log("¡El pool ACEPTÓ el bloque! Búscalo en https://mempool.space con el hash "
                         f"{share['hash']}. La recompensa podrá gastarse tras 100 confirmaciones (~17 h).")
        else:
            self.rejected += 1
            reason = format_error(error) if error else "sin motivo"
            self.log(f"Share rechazada: {reason}")

    # --- estadísticas -------------------------------------------------------------

    def _print_stats(self, final: bool = False) -> None:
        if self.pool is None:
            return
        hashrate = self._meter.update()
        if final:
            self.log(f"Resumen: {self.accepted} shares aceptadas, {self.rejected} rechazadas, "
                     f"bloques encontrados: {self.blocks_found}.")
            return
        line = (f"Velocidad: {stats.format_hashrate(hashrate)} | Shares: {self.accepted} aceptadas, "
                f"{self.rejected} rechazadas")
        if self.best_difficulty:
            line += f" | Mejor share: {stats.format_difficulty(self.best_difficulty)}"
        self.log(line)
        if self.network_difficulty and hashrate > 0:
            seconds = stats.expected_seconds_to_block(hashrate, self.network_difficulty)
            year = stats.probability_within(stats.SECONDS_PER_YEAR, hashrate, self.network_difficulty)
            self.log(f"Tiempo medio hasta un bloque: {stats.format_duration(seconds)} | "
                     f"Probabilidad en 1 año: {stats.format_odds(year)}")
        alive = self.pool.alive()
        if alive < self.pool.threads:
            self.log(f"ATENCIÓN: solo quedan {alive} de {self.pool.threads} procesos de minado vivos.")


def run_benchmark(threads: int, seconds: float, low_priority: bool, log=log_line) -> float:
    """Mide la velocidad de tu CPU sin conectarse a ningún pool."""
    pool = WorkerPool(threads, low_priority)
    pool.start()
    job = {
        "seq": 0, "job_id": "benchmark", "prevhash": "00" * 32,
        "coinb1": bytes(40), "coinb2": bytes(40), "merkle_branch": [bytes(32)] * 12,
        "version": "20000000", "nbits": "1d00ffff", "ntime": f"{int(time.time()):08x}",
        "extranonce1": bytes(4), "extranonce2_size": 4,
        # Objetivo imposible: solo se mide la velocidad.
        "share_target": 0, "network_target": 0,
    }
    try:
        log(f"Midiendo la velocidad con {threads} procesos durante {seconds:.0f} s...")
        pool.dispatch(job)
        time.sleep(3)  # dejar que arranquen todos los procesos
        meter = HashrateMeter(pool)
        time.sleep(seconds)
        hashrate = meter.update()
    finally:
        pool.stop()
    difficulty = REFERENCE_NETWORK_DIFFICULTY
    expected = stats.expected_seconds_to_block(hashrate, difficulty)
    year = stats.probability_within(stats.SECONDS_PER_YEAR, hashrate, difficulty)
    reward = BLOCK_SUBSIDY_SATS / SATS_PER_BTC
    log(f"Tu velocidad: {stats.format_hashrate(hashrate)}")
    log(f"Con la dificultad de la red de octubre de 2026 (~{stats.format_difficulty(difficulty)}):")
    log(f"  Tiempo medio hasta encontrar un bloque: {stats.format_duration(expected)}")
    log(f"  Probabilidad de encontrar un bloque en 1 año: {stats.format_odds(year)}")
    log(f"  Premio si lo encuentras: {reward} BTC + comisiones de las transacciones")
    return hashrate
