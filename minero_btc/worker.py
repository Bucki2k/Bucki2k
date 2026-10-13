"""Proceso trabajador: prueba nonces lo más rápido que permite Python.

Cada proceso usa un núcleo. Recibe trabajos por su cola, va cambiando el
extranonce2 (cada proceso usa valores distintos para no repetir trabajo) y
recorre los 2^32 nonces de cada cabecera. Optimización "midstate": los
primeros 64 bytes de la cabecera no cambian con el nonce, así que su estado
SHA-256 se calcula una vez y se copia para cada nonce.
"""

from __future__ import annotations

import hashlib
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
_ZERO4 = b"\x00\x00\x00\x00"
_TOP_32_BITS = 1 << 224


def scan(prefix76: bytes, start: int, end: int, report_target: int) -> list[tuple[int, bytes]]:
    """Prueba los nonces ``[start, end)`` y devuelve los ``(nonce, hash)`` con hash <= objetivo."""
    sha256 = hashlib.sha256
    pack = struct.Struct("<I").pack
    copy = sha256(prefix76[:64]).copy
    tail = prefix76[64:76]
    found = []
    if report_target < _TOP_32_BITS:
        # Camino rápido: un hash que cumple tiene sus 4 bytes más altos a cero.
        for nonce in range(start, end):
            h = copy()
            h.update(tail + pack(nonce))
            digest = sha256(h.digest()).digest()
            if digest.endswith(_ZERO4) and int.from_bytes(digest, "little") <= report_target:
                found.append((nonce, digest))
    else:
        for nonce in range(start, end):
            h = copy()
            h.update(tail + pack(nonce))
            digest = sha256(h.digest()).digest()
            if int.from_bytes(digest, "little") <= report_target:
                found.append((nonce, digest))
    return found


def lower_priority() -> None:
    """Prioridad mínima: el PC sigue respondiendo (juegos, navegador...)."""
    try:
        if sys.platform == "win32":
            import ctypes
            idle_priority_class = 0x00000040
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), idle_priority_class)
        else:
            os.nice(19)
    except (OSError, AttributeError):
        pass


def worker_main(index, n_workers, job_queue, result_queue, hash_counter, stop_event, low_priority):
    try:
        if low_priority:
            lower_priority()
        _work(index, n_workers, job_queue, result_queue, hash_counter, stop_event)
    except KeyboardInterrupt:
        pass


def _next_message(job_queue, block: bool):
    try:
        return job_queue.get(timeout=0.5) if block else job_queue.get_nowait()
    except queue.Empty:
        return None


def _work(index, n_workers, job_queue, result_queue, hash_counter, stop_event) -> None:
    message = None
    while not stop_event.is_set():
        if message is None:
            message = _next_message(job_queue, block=True)
            continue
        if message == "stop":
            return
        if message == "pause":
            message = None
            continue
        message = _mine_job(message, index, n_workers, job_queue, result_queue, hash_counter, stop_event)


def _mine_job(job, index, n_workers, job_queue, result_queue, hash_counter, stop_event):
    """Mina ``job`` hasta que llegue otro mensaje por la cola y lo devuelve (None si no hay)."""
    size = job["extranonce2_size"]
    share_target = job["share_target"]
    network_target = job["network_target"]
    # También informamos de hashes de dificultad >= 1 para la estadística de "mejor share".
    report_target = max(share_target, DIFF1_TARGET)
    counter = 0
    while True:
        extranonce2_value = index + counter * n_workers
        counter += 1
        if extranonce2_value >= 1 << (8 * size):
            return None  # espacio de extranonce2 agotado: esperar trabajo nuevo
        extranonce2 = extranonce2_value.to_bytes(size, "big")
        coinbase = build_coinbase(job["coinb1"], job["extranonce1"], extranonce2, job["coinb2"])
        prefix = build_header_prefix(job["version"], job["prevhash"],
                                     merkle_root(coinbase, job["merkle_branch"]),
                                     job["ntime"], job["nbits"])
        for start in range(0, NONCE_LIMIT, CHUNK):
            for nonce, digest in scan(prefix, start, start + CHUNK, report_target):
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
            with hash_counter.get_lock():
                hash_counter.value += CHUNK
            if stop_event.is_set():
                return None
            message = _next_message(job_queue, block=False)
            if message is not None:
                return message
