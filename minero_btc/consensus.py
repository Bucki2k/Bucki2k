"""Reglas de Bitcoin necesarias para minar.

Doble SHA-256, objetivos (targets) y dificultad, raíz Merkle, cabecera del
bloque a partir de un trabajo Stratum y lectura de las salidas de la
transacción coinbase que construye el pool (para comprobar a quién paga).

Orden de bytes, que es donde suelen fallar los mineros caseros:
  * Los hashes se comparan como enteros little-endian (``int.from_bytes(h, "little")``).
  * Stratum envía ``version``, ``nbits`` y ``ntime`` como hex big-endian; en la
    cabecera van little-endian.
  * Stratum envía ``prevhash`` con cada palabra de 4 bytes invertida; hay que
    volver a invertir cada palabra para obtener los bytes internos.
  * El nonce va little-endian en la cabecera y se envía al pool como hex
    big-endian de su valor (``"%08x" % nonce``).
"""

from __future__ import annotations

import hashlib
import struct
from fractions import Fraction

# Objetivo de dificultad 1 ("pdiff"); es la referencia de los pools para las shares.
DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
MAX_TARGET = (1 << 256) - 1

# Recompensa base por bloque desde el halving de abril de 2024 (en satoshis).
BLOCK_SUBSIDY_SATS = 312_500_000
SATS_PER_BTC = 100_000_000


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash_to_int(digest: bytes) -> int:
    return int.from_bytes(digest, "little")


def hash_to_hex(digest: bytes) -> str:
    """Hash en el formato que muestran los exploradores de bloques."""
    return digest[::-1].hex()


def bits_to_target(nbits: int) -> int:
    """Decodifica el campo compacto ``nbits`` de la cabecera."""
    exponent = nbits >> 24
    mantissa = nbits & 0x007FFFFF
    if nbits & 0x00800000:
        raise ValueError("nbits negativo")
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def target_to_difficulty(target: int) -> float:
    if target <= 0:
        raise ValueError("objetivo no válido")
    return DIFF1_TARGET / target


def difficulty_to_target(difficulty: float) -> int:
    if difficulty <= 0:
        raise ValueError("la dificultad debe ser positiva")
    target = int(Fraction(DIFF1_TARGET) / Fraction(difficulty))
    return min(max(target, 1), MAX_TARGET)


def stratum_prevhash_to_bytes(prevhash_hex: str) -> bytes:
    raw = bytes.fromhex(prevhash_hex)
    if len(raw) != 32:
        raise ValueError("prevhash debe tener 32 bytes")
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 32, 4))


def bytes_to_stratum_prevhash(prevhash: bytes) -> str:
    """Inversa de :func:`stratum_prevhash_to_bytes` (útil para pools de prueba)."""
    return stratum_prevhash_to_bytes(prevhash.hex()).hex()


def build_coinbase(coinb1: bytes, extranonce1: bytes, extranonce2: bytes, coinb2: bytes) -> bytes:
    return coinb1 + extranonce1 + extranonce2 + coinb2


def merkle_root(coinbase: bytes, branches: list[bytes]) -> bytes:
    root = sha256d(coinbase)
    for branch in branches:
        root = sha256d(root + branch)
    return root


def build_header_prefix(version_hex: str, prevhash_hex: str, merkle: bytes,
                        ntime_hex: str, nbits_hex: str) -> bytes:
    """Los primeros 76 bytes de la cabecera (todo salvo el nonce)."""
    if len(merkle) != 32:
        raise ValueError("la raíz Merkle debe tener 32 bytes")
    return (struct.pack("<I", int(version_hex, 16))
            + stratum_prevhash_to_bytes(prevhash_hex)
            + merkle
            + struct.pack("<I", int(ntime_hex, 16))
            + struct.pack("<I", int(nbits_hex, 16)))


def nonce_to_hex(nonce: int) -> str:
    return f"{nonce:08x}"


# --- Lectura de la coinbase -------------------------------------------------


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    if first < 0xFD:
        return first, pos + 1
    size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    end = pos + 1 + size
    if end > len(data):
        raise ValueError("varint truncado")
    return int.from_bytes(data[pos + 1:end], "little"), end


def _take(data: bytes, pos: int, size: int) -> tuple[bytes, int]:
    end = pos + size
    if size < 0 or end > len(data):
        raise ValueError("transacción truncada")
    return data[pos:end], end


def parse_coinbase_outputs(tx: bytes) -> list[tuple[int, bytes]]:
    """Devuelve ``[(satoshis, scriptPubKey), ...]`` de una transacción."""
    pos = 4  # versión
    segwit = len(tx) > 6 and tx[4] == 0 and tx[5] == 1
    if segwit:
        pos += 2
    n_inputs, pos = _read_varint(tx, pos)
    for _ in range(n_inputs):
        _, pos = _take(tx, pos, 36)  # outpoint
        script_len, pos = _read_varint(tx, pos)
        _, pos = _take(tx, pos, script_len)
        _, pos = _take(tx, pos, 4)  # sequence
    n_outputs, pos = _read_varint(tx, pos)
    outputs = []
    for _ in range(n_outputs):
        value_bytes, pos = _take(tx, pos, 8)
        script_len, pos = _read_varint(tx, pos)
        script, pos = _take(tx, pos, script_len)
        outputs.append((int.from_bytes(value_bytes, "little"), script))
    if not segwit and pos + 4 != len(tx):
        raise ValueError("la coinbase tiene bytes de más o de menos")
    return outputs


def coinbase_payout(tx: bytes, script_pubkey: bytes) -> tuple[int, int]:
    """Satoshis que la coinbase paga a ``script_pubkey`` y total de la coinbase."""
    outputs = parse_coinbase_outputs(tx)
    total = sum(value for value, _ in outputs)
    mine = sum(value for value, script in outputs if script == script_pubkey)
    return mine, total
