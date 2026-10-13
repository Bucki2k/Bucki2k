"""Proceso trabajador: prueba nonces lo más rápido que permite Python.

Cada proceso usa un núcleo. Recibe trabajos por su cola, va cambiando el
extranonce2 (cada proceso usa valores distintos para no repetir trabajo) y
recorre los 2^32 nonces de cada cabecera. Optimización "midstate": los
primeros 76 bytes de la cabecera no cambian con el nonce, así que su estado
SHA-256 se calcula una vez y se copia para cada nonce.

Para no quedarse bloqueado si se pulsa Ctrl+C justo en mal momento, el bucle
principal no usa ningún candado compartido: cada proceso escribe solo su
propio contador de hashes y lee una bandera de parada sin candado.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import struct
import sys

from .consensus import (
    DIFF1_TARGET,
    build_coinbase,
    build_header_prefix,
    hash_to_hex,
    merkle_root,
    nonce_to_hex,
)

CHUNK = 1 << 15  # nonces entre comprobaciones de trabajo nuevo (~30 ms)
NONCE_LIMIT = 1 << 32
PARENT_CHECK_CHUNKS = 32  # ~1 s entre comprobaciones de que el proceso principal sigue vivo
MAX_REPORTS_PER_CHUNK = 16  # tope de resultados por bloque de nonces (se guardan los mejores)
_TOP_8_BITS = 1 << 248


def scan(prefix76: bytes, start: int, end: int, report_target: int) -> list[tuple[int, bytes]]:
    """Prueba los nonces ``[start, end)`` y devuelve los ``(nonce, hash)`` con hash <= objetivo."""
    sha256 = hashlib.sha256
    pack = struct.Struct("<I").pack
    # Midstate: 64 bytes ya comprimidos y los 12 siguientes en el búfer.
    copy = sha256(prefix76).copy
    # Copiar un contexto vacío es más barato que llamar a sha256(datos).
    empty = sha256().copy
    found = []
    if report_target < _TOP_8_BITS:
        # Un hash que cumple tiene a cero su byte más alto (digest[31]).
        for nonce in range(start, end):
            h = copy()
            h.update(pack(nonce))
            h2 = empty()
            h2.update(h.digest())
            digest = h2.digest()
            if not digest[31] and int.from_bytes(digest, "little") <= report_target:
                found.append((nonce, digest))
    else:
        for nonce in range(start, end):
            h = copy()
            h.update(pack(nonce))
            h2 = empty()
            h2.update(h.digest())
            digest = h2.digest()
            if int.from_bytes(digest, "little") <= report_target:
                found.append((nonce, digest))
    return found


def lower_priority() -> None:
    """Prioridad mínima: el PC sigue respondiendo mientras mina."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            idle_priority_class = 0x00000040
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), idle_priority_class)
        else:
            os.nice(19)
    except (OSError, AttributeError):
        pass


def worker_main(index, n_workers, job_queue, result_queue, hash_counts, stop_flag, low_priority):
    try:
        if low_priority:
            lower_priority()
        _work(index, n_workers, job_queue, result_queue, hash_counts, stop_flag)
    except KeyboardInterrupt:
        pass
    finally:
        # Nada de lo que quede en la cola se va a enviar ya: no esperar a vaciarla.
        result_queue.cancel_join_thread()


def _parent_alive() -> bool:
    parent = multiprocessing.parent_process()
    return parent is None or parent.is_alive()


def _next_message(job_queue, block: bool):
    try:
        return job_queue.get(timeout=0.5) if block else job_queue.get_nowait()
    except queue.Empty:
        return None


def _work(index, n_workers, job_queue, result_queue, hash_counts, stop_flag) -> None:
    message = None
    # El contador de extranonce2 sigue entre trabajos: si el pool repite un
    # trabajo (por ejemplo tras reconectar), no se repiten shares ya enviadas.
    state = {"extranonce2_counter": 0}
    while not stop_flag.value:
        if message is None:
            message = _next_message(job_queue, block=True)
            if message is None and not _parent_alive():
                return
            continue
        if message == "stop":
            return
        if message == "pause":
            message = None
            continue
        try:
            message = _mine_job(message, index, n_workers, job_queue, result_queue,
                                hash_counts, stop_flag, state)
        except Exception as exc:  # trabajo malformado: descartarlo y esperar el siguiente
            result_queue.put({"error": f"{type(exc).__name__}: {exc}"[:300]})
            message = None


def _mine_job(job, index, n_workers, job_queue, result_queue, hash_counts, stop_flag, state):
    """Mina ``job`` hasta que llegue otro mensaje por la cola y lo devuelve (None si no hay)."""
    size = job["extranonce2_size"]
    share_target = job["share_target"]
    network_target = job["network_target"]
    # También informamos de hashes de dificultad >= 1 para la estadística de "mejor share".
    report_target = max(share_target, network_target, DIFF1_TARGET)
    space = 1 << (8 * size)
    # Valores de extranonce2 de este proceso: index, index + n, index + 2n... < space.
    own_values = (space - index + n_workers - 1) // n_workers
    tried = 0
    chunks = 0
    while True:
        if tried >= own_values:
            return None  # todos probados en este trabajo: esperar uno nuevo
        extranonce2_value = index + state["extranonce2_counter"] * n_workers
        if extranonce2_value >= space:
            state["extranonce2_counter"] = 0
            extranonce2_value = index
        state["extranonce2_counter"] += 1
        tried += 1
        extranonce2 = extranonce2_value.to_bytes(size, "big")
        coinbase = build_coinbase(job["coinb1"], job["extranonce1"], extranonce2, job["coinb2"])
        prefix = build_header_prefix(job["version"], job["prevhash"],
                                     merkle_root(coinbase, job["merkle_branch"]),
                                     job["ntime"], job["nbits"])
        for start in range(0, NONCE_LIMIT, CHUNK):
            found = scan(prefix, start, start + CHUNK, report_target)
            if len(found) > MAX_REPORTS_PER_CHUNK:
                # Solo pasa con objetivos absurdos: quedarse con los mejores hashes.
                found.sort(key=lambda item: int.from_bytes(item[1], "little"))
                del found[MAX_REPORTS_PER_CHUNK:]
            for nonce, digest in found:
                value = int.from_bytes(digest, "little")
                result_queue.put({
                    "seq": job["seq"],
                    "extranonce2": extranonce2.hex(),
                    "ntime": job["ntime"],
                    "nonce": nonce_to_hex(nonce),
                    "hash": hash_to_hex(digest),
                    "difficulty": DIFF1_TARGET / value if value else float("inf"),
                    "is_share": value <= share_target,
                    "is_block": value <= network_target,
                })
            hash_counts[index] += CHUNK
            chunks += 1
            if stop_flag.value:
                return "stop"
            if chunks % PARENT_CHECK_CHUNKS == 0 and not _parent_alive():
                return "stop"
            message = _next_message(job_queue, block=False)
            if message is not None:
                return message
