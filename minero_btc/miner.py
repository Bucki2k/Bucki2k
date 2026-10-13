"""Coordinador: conecta con el pool, reparte trabajo entre los procesos,
envía las shares y muestra estadísticas."""

from __future__ import annotations

import itertools
import math
import multiprocessing
import os
import queue
import socket
import ssl
import sys
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
from .stratum import StratumClient, StratumError, format_error, parse_hex, pool_text
from .worker import worker_main

# Dificultad de la red aproximada (octubre de 2026). Solo se usa en la prueba
# de velocidad sin conexión; minando se usa la dificultad real de cada trabajo.
REFERENCE_NETWORK_DIFFICULTY = 132.7e12
MAX_VALID_JOBS = 16
# Por debajo de este porcentaje de la recompensa no es minería en solitario
# (ckpool se queda un 2 %, public-pool nada).
MIN_PAYOUT_PERCENT = 95.0
STABLE_SESSION_SECONDS = 60.0
# La dificultad de Bitcoin supera esto desde 2019: un trabajo por debajo es falso
# (un pool que miente para mostrar "bloques" falsos o inundar el minero).
MIN_NETWORK_DIFFICULTY = 1e12


def log_line(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def btc(sats: int) -> str:
    return f"{sats / SATS_PER_BTC:.8f} BTC"


def describe_error(exc: BaseException, host: str) -> str:
    """Explicación en español de los fallos de conexión más habituales."""
    if isinstance(exc, socket.gaierror):
        return f"no se encuentra el servidor {host} (¿sin Internet o nombre mal escrito?)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "el pool no responde (¿sin Internet?)"
    if isinstance(exc, ConnectionRefusedError):
        return "el pool rechazó la conexión (¿puerto correcto?)"
    if isinstance(exc, ssl.SSLError):
        return "error de TLS (¿ese puerto admite stratum+ssl?)"
    return pool_text(exc)


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
    # Nunca se envían shares más fáciles que esto, aunque el pool lo permita:
    # en solitario no dan dinero y así un pool no puede inundar la memoria.
    min_share_difficulty: float = 1.0
    stats_interval: float = 30.0
    low_priority: bool = True
    prevent_sleep: bool = True

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
        # Sin candados: cada proceso escribe solo su contador.
        self.hash_counts = ctx.RawArray("Q", threads)
        self._stop_flag = ctx.RawValue("b", 0)
        self._queues = [ctx.Queue() for _ in range(threads)]
        self._processes = [
            ctx.Process(
                target=worker_main,
                args=(i, threads, self._queues[i], self.results, self.hash_counts,
                      self._stop_flag, low_priority),
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
        return sum(self.hash_counts)

    def alive(self) -> int:
        return sum(process.is_alive() for process in self._processes)

    def stop(self) -> None:
        self._stop_flag.value = 1
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
        self.hashrate = 0.0
        self.reset()

    def reset(self) -> None:
        self._last_hashes = self._pool.total_hashes()
        self._last_time = time.monotonic()

    def update(self) -> float:
        now = time.monotonic()
        hashes = self._pool.total_hashes()
        elapsed = now - self._last_time
        if elapsed > 0:
            self.hashrate = (hashes - self._last_hashes) / elapsed
        self._last_hashes, self._last_time = hashes, now
        return self.hashrate


class _PreventSleep:
    """Evita que Windows entre en suspensión mientras se mina (la pantalla sí puede apagarse)."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self, enabled: bool):
        self.active = False
        if not enabled or sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self._set = ctypes.windll.kernel32.SetThreadExecutionState
            self._set.argtypes = [wintypes.DWORD]
            self._set.restype = wintypes.DWORD
            self.active = bool(self._set(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED))
        except (AttributeError, OSError):
            self.active = False

    def release(self) -> None:
        if self.active:
            self._set(self.ES_CONTINUOUS)
            self.active = False


class Miner:
    reconnect_delay = 5.0  # segundos; se duplica en cada fallo seguido (máx. 2 min)
    min_network_difficulty = MIN_NETWORK_DIFFICULTY

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
        self.difficulty = 1.0
        self.extranonce1 = b""
        self.extranonce2_size = 0
        self._floor_target = difficulty_to_target(config.min_share_difficulty)
        self._payout_kind: str | None = None
        self._payout_pct: float | None = None
        self._handshake_ok = False
        self._connected_at = 0.0
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
        no_sleep = _PreventSleep(cfg.prevent_sleep)
        if no_sleep.active:
            self.log("Windows no entrará en suspensión mientras el minero esté en marcha "
                     "(la pantalla sí puede apagarse).")
        backoff = self.reconnect_delay
        try:
            while not self.stop_event.is_set():
                self._handshake_ok = False
                reconnect_wait = None
                try:
                    reconnect_wait = self._session()
                except (OSError, StratumError) as exc:
                    self.log(f"Problema con el pool: {describe_error(exc, cfg.host)}")
                except Exception as exc:  # nunca dejar de minar por un mensaje raro del pool
                    self._handshake_ok = False
                    self.log(f"Error inesperado ({type(exc).__name__}: {pool_text(exc)}); se reconecta.")
                if self.stop_event.is_set():
                    break
                self.pool.pause()
                stable = (self._handshake_ok
                          and time.monotonic() - self._connected_at >= STABLE_SESSION_SECONDS)
                if stable:
                    backoff = self.reconnect_delay
                    wait = reconnect_wait if reconnect_wait is not None else backoff
                else:
                    wait = max(reconnect_wait or 0.0, backoff)
                    backoff = min(backoff * 2, 120.0)
                self.log(f"Reconectando en {wait:.0f} s...")
                self._wait(wait)
        except KeyboardInterrupt:
            pass
        finally:
            no_sleep.release()
            self.log("Deteniendo los procesos de minado...")
            self.pool.stop()
            self._print_stats(final=True)

    def _wait(self, seconds: float) -> None:
        # Esperas cortas: en Windows Ctrl+C no interrumpe una espera larga.
        deadline = time.monotonic() + seconds
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 0.25))

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
            self._connected_at = time.monotonic()
            self.log(f"Conectado. Minando para {cfg.address} (trabajador '{cfg.worker_name}').")
            self.difficulty = 1.0
            self.extranonce1 = client.extranonce1
            self.extranonce2_size = client.extranonce2_size
            # Sin conexión los procesos estaban parados: no contar ese tiempo.
            self._meter.reset()
            self._next_stats = time.monotonic() + cfg.stats_interval
            while not self.stop_event.is_set():
                try:
                    event = client.events.get(timeout=0.2)
                except queue.Empty:
                    event = None
                if event is not None:
                    try:
                        outcome = self._handle_event(client, event)
                    except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
                        self.log(f"Mensaje no válido del pool ignorado ({type(exc).__name__}).")
                        outcome = None
                    if outcome is not None:
                        self._drain_results(client)
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
            except (TypeError, ValueError, OverflowError):
                value = math.nan
            if not (math.isfinite(value) and value > 0):
                self.log(f"Dificultad no válida del pool ignorada: {pool_text(event[1], 40)}")
            else:
                if value != self.difficulty:
                    self.log(f"El pool fija la dificultad de share en {stats.format_difficulty(value)}.")
                self.difficulty = value  # se aplica a partir del siguiente trabajo
        elif kind == "set_extranonce":
            params = event[1]
            try:
                if not isinstance(params, list) or len(params) < 2:
                    raise StratumError("parámetros incorrectos")
                client._set_extranonce(params[0], params[1])
            except StratumError as exc:
                self.log(f"set_extranonce no válido, se ignora: {exc}")
            else:
                self.extranonce1 = client.extranonce1
                self.extranonce2_size = client.extranonce2_size
        elif kind == "response":
            self._on_response(event[1], event[2], event[3])
        elif kind == "message":
            self.log(f"Mensaje del pool: {pool_text(event[1])}")
        elif kind == "reconnect":
            params = event[1] if isinstance(event[1], list) else []
            wait = 5.0
            if len(params) >= 3:
                try:
                    wait = float(params[2])
                except (TypeError, ValueError, OverflowError):
                    pass
            wait = min(max(wait, 5.0), 300.0) if math.isfinite(wait) else 5.0
            self.log("El pool pide reconectar.")
            return wait
        elif kind == "disconnected":
            raise ConnectionError(event[1])
        return None

    def _on_notify(self, params) -> None:
        try:
            if not isinstance(params, list) or len(params) < 9:
                raise ValueError("faltan campos")
            (job_id, prevhash, coinb1, coinb2, branches,
             version, nbits, ntime, clean) = params[:9]
            if isinstance(job_id, bool) or not isinstance(job_id, (str, int)):
                raise ValueError("job_id no válido")
            if not isinstance(branches, list):
                raise ValueError("rama Merkle no válida")
            parse_hex(prevhash, 32)
            for value in (version, nbits, ntime):
                parse_hex(value, 4)
            network_target = bits_to_target(int(nbits, 16))
            if network_target <= 0:
                raise ValueError("nbits no válido")
            network_difficulty = target_to_difficulty(network_target)
            if network_difficulty < self.min_network_difficulty:
                raise ValueError(f"dificultad de red imposible ({stats.format_difficulty(network_difficulty)}); "
                                 "el pool podría estar mintiendo")
            # Shares nunca más fáciles que el mínimo local, y todo bloque cuenta como share.
            share_target = min(difficulty_to_target(self.difficulty), self._floor_target)
            job = {
                "seq": next(self._seq),
                "job_id": str(job_id),
                "prevhash": prevhash,
                "coinb1": parse_hex(coinb1),
                "coinb2": parse_hex(coinb2),
                "merkle_branch": [parse_hex(b, 32) for b in branches],
                "version": version,
                "nbits": nbits,
                "ntime": ntime,
                "extranonce1": self.extranonce1,
                "extranonce2_size": self.extranonce2_size,
                "share_target": max(share_target, network_target),
                "network_target": network_target,
            }
        except (TypeError, ValueError, OverflowError) as exc:
            self.log(f"Trabajo no válido recibido del pool, se ignora ({pool_text(exc)}).")
            return
        if clean:
            self._jobs.clear()
        self._jobs[job["seq"]] = job
        while len(self._jobs) > MAX_VALID_JOBS:
            self._jobs.pop(next(iter(self._jobs)))
        if network_difficulty != self.network_difficulty:
            self.log(f"Dificultad de la red: {stats.format_difficulty(network_difficulty)} "
                     "(un bloque necesita un hash al menos así de difícil).")
        self.network_difficulty = network_difficulty
        self._check_payout(job)
        self.pool.dispatch(job)

    def _check_payout(self, job) -> None:
        """Comprueba qué parte de la recompensa paga la coinbase del pool a tu dirección."""
        coinbase = build_coinbase(job["coinb1"], job["extranonce1"],
                                  b"\x00" * job["extranonce2_size"], job["coinb2"])
        try:
            mine, total = coinbase_payout(coinbase, self.config.script_pubkey)
        except (ValueError, KeyError, IndexError):
            mine, total = None, 0
        if mine is None:
            kind, pct = "unreadable", None
        elif mine == 0 or not total:
            kind, pct = "not_mine", 0.0
        else:
            pct = mine / total * 100
            kind = "mine" if pct >= MIN_PAYOUT_PERCENT else "low"
        unchanged = (kind == self._payout_kind
                     and (pct is None or self._payout_pct is None or abs(pct - self._payout_pct) < 1.0))
        if unchanged:
            return
        self._payout_kind, self._payout_pct = kind, pct
        if kind == "mine":
            self.log(f"Comprobado: si encuentras este bloque, {btc(mine)} ({pct:.1f} % de la "
                     f"recompensa) van DIRECTAMENTE a tu dirección {self.config.address}.")
            if pct < 99.9:
                self.log(f"El resto ({100 - pct:.1f} %) es la comisión del pool.")
        else:
            self.log(self._payout_warning())

    def _payout_warning(self) -> str:
        if self._payout_kind == "unreadable":
            return ("ATENCIÓN: no se pudo leer el bloque que prepara el pool para comprobar "
                    "a quién paga.")
        if self._payout_kind == "low":
            return (f"ATENCIÓN: este pool solo te pagaría el {self._payout_pct:.1f} % del bloque; "
                    "el resto va a otras direcciones. Esto NO es minería en solitario.")
        return ("ATENCIÓN: el bloque que prepara este pool NO paga a tu dirección. Este pool no "
                "es de minería en solitario (o algo va mal). Usa public-pool.io:3333 o "
                "solo.ckpool.org:3333 si quieres que la recompensa llegue directa a tu wallet.")

    # --- resultados de los procesos ----------------------------------------------

    def _drain_results(self, client: StratumClient) -> None:
        while True:
            try:
                result = self.pool.results.get_nowait()
            except queue.Empty:
                return
            if "error" in result:
                self.log(f"Un proceso descartó un trabajo no válido: {pool_text(result['error'])}")
                continue
            self.best_difficulty = max(self.best_difficulty, result["difficulty"])
            if not (result["is_share"] or result["is_block"]):
                continue
            job = self._jobs.get(result["seq"])
            if job is None:
                self.stale += 1
                if result["is_block"]:
                    self.log(f"Se encontró un bloque ({result['hash']}) para un trabajo que ya no "
                             "es válido: la red ya había avanzado o se cortó la conexión.")
                continue
            try:
                request_id = client.submit(self.config.username, job["job_id"], result["extranonce2"],
                                           result["ntime"], result["nonce"])
            except OSError:
                if result["is_block"]:
                    self.log(f"¡Bloque encontrado pero no se pudo enviar! job={job['job_id']} "
                             f"extranonce1={job['extranonce1'].hex()} extranonce2={result['extranonce2']} "
                             f"ntime={result['ntime']} nonce={result['nonce']} hash={result['hash']}")
                raise
            self._pending[request_id] = result
            if result["is_block"]:
                self.blocks_found += 1
                self.log("*" * 70)
                self.log(f"¡¡¡BLOQUE ENCONTRADO!!! Hash {result['hash']}")
                self.log("Enviado al pool, esperando respuesta...")
                self.log("*" * 70)

    def _on_response(self, request_id, result, error) -> None:
        share = self._pending.pop(request_id, None)
        if share is None:
            return  # respuesta a otra petición (p. ej. suggest_difficulty)
        if result is True:
            self.accepted += 1
            if share["is_block"]:
                self.log("¡El pool ACEPTÓ el bloque! Búscalo en https://mempool.space con el hash "
                         f"{share['hash']}. La recompensa podrá gastarse tras 100 confirmaciones (~17 h). "
                         "Nadie te pedirá nunca tu frase semilla para cobrarlo.")
        else:
            self.rejected += 1
            reason = format_error(error) if error else "sin motivo"
            self.log(f"{'BLOQUE' if share['is_block'] else 'Share'} rechazado por el pool: {reason}")

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
        if self._payout_kind not in (None, "mine"):
            self.log(self._payout_warning())
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
    log(f"Tu velocidad: {stats.format_hashrate(hashrate)}")
    log(f"Con la dificultad de la red de octubre de 2026 (~{stats.format_difficulty(difficulty)}):")
    log(f"  Tiempo medio hasta encontrar un bloque: {stats.format_duration(expected)}")
    log(f"  Probabilidad de encontrar un bloque en 1 año: {stats.format_odds(year)}")
    log(f"  Premio si lo encuentras: {btc(BLOCK_SUBSIDY_SATS)} + comisiones de las transacciones")
    return hashrate
