"""Validación de direcciones de Bitcoin (mainnet) y detección de secretos.

El minero solo necesita la dirección PÚBLICA de tu wallet. Si alguien escribe
por error su frase semilla o su clave privada, nos negamos a usarla.

Se aceptan:
  * P2PKH   ``1...``   (Base58Check, versión 0x00)
  * P2SH    ``3...``   (Base58Check, versión 0x05)
  * SegWit  ``bc1q...`` (bech32, BIP173) y Taproot ``bc1p...`` (bech32m, BIP350)
"""

from __future__ import annotations

import hashlib
import re

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3


class AddressError(ValueError):
    """La dirección no es válida o no es de Bitcoin mainnet."""


class SecretDetectedError(ValueError):
    """El texto parece una frase semilla o una clave privada."""


def _sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# --- Base58Check -------------------------------------------------------------


def base58check_decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = BASE58_ALPHABET.find(char)
        if index < 0:
            raise AddressError(f"tiene un carácter que no puede estar en una dirección de Bitcoin: {char!r}")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(text) - len(text.lstrip("1"))
    raw = b"\x00" * leading_zeros + body
    if len(raw) < 5:
        raise AddressError("dirección demasiado corta")
    payload, checksum = raw[:-4], raw[-4:]
    if _sha256d(payload)[:4] != checksum:
        raise AddressError("la suma de verificación no coincide (¿hay una letra mal copiada?)")
    return payload


# --- Bech32 / Bech32m (código de referencia de BIP173/BIP350) ----------------


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(text: str) -> tuple[str, list[int], int]:
    if any(ord(c) < 33 or ord(c) > 126 for c in text):
        raise AddressError("carácter no válido")
    if text.lower() != text and text.upper() != text:
        raise AddressError("mezcla de mayúsculas y minúsculas")
    text = text.lower()
    sep = text.rfind("1")
    if sep < 1 or sep + 7 > len(text) or len(text) > 90:
        raise AddressError("formato bech32 no válido")
    hrp = text[:sep]
    data = []
    for char in text[sep + 1:]:
        index = BECH32_CHARSET.find(char)
        if index < 0:
            raise AddressError(f"tiene un carácter que no puede estar en una dirección bc1: {char!r}")
        data.append(index)
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const not in (BECH32_CONST, BECH32M_CONST):
        raise AddressError("la suma de verificación no coincide (¿hay una letra mal copiada?)")
    return hrp, data[:-6], const


def _convertbits(data: list[int], frombits: int, tobits: int) -> bytes:
    acc = 0
    bits = 0
    out = bytearray()
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise AddressError("relleno bech32 no válido")
    return bytes(out)


def _decode_segwit(text: str) -> tuple[int, bytes]:
    hrp, data, const = _bech32_decode(text)
    if hrp in ("tb", "bcrt"):
        raise AddressError("es una dirección de testnet/regtest; necesitas una de Bitcoin real (bc1...)")
    if hrp != "bc":
        raise AddressError("no es una dirección de Bitcoin")
    if not data:
        raise AddressError("dirección vacía")
    version = data[0]
    if version > 16:
        raise AddressError("versión segwit no válida")
    program = _convertbits(data[1:], 5, 8)
    if not 2 <= len(program) <= 40:
        raise AddressError("longitud de programa segwit no válida")
    if version == 0 and len(program) not in (20, 32):
        raise AddressError("longitud de programa segwit v0 no válida")
    expected = BECH32_CONST if version == 0 else BECH32M_CONST
    if const != expected:
        raise AddressError("codificación bech32/bech32m incorrecta para esta versión")
    # Las salidas de versiones futuras (o Taproot con otra longitud) hoy no
    # tienen dueño: cualquiera podría gastar una recompensa enviada ahí.
    if version > 1 or (version == 1 and len(program) != 32):
        raise AddressError("tipo de dirección segwit no soportado; usa una bc1q o bc1p normal")
    return version, program


# --- API pública ---------------------------------------------------------------


def address_to_script_pubkey(address: str) -> bytes:
    """Valida ``address`` y devuelve el scriptPubKey al que paga."""
    address = address.strip()
    if address.lower().startswith(("bc1", "tb1", "bcrt1")):
        version, program = _decode_segwit(address)
        opcode = 0x50 + version if version else 0x00
        return bytes([opcode, len(program)]) + program
    payload = base58check_decode(address)
    if len(payload) != 21:
        raise AddressError("longitud de dirección no válida")
    version, hash160 = payload[0], payload[1:]
    if version == 0x00:
        return b"\x76\xa9\x14" + hash160 + b"\x88\xac"
    if version == 0x05:
        return b"\xa9\x14" + hash160 + b"\x87"
    if version in (0x6F, 0xC4):
        raise AddressError("es una dirección de testnet; necesitas una de Bitcoin real")
    raise AddressError("no es una dirección de Bitcoin")


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def looks_like_secret(text: str) -> bool:
    """¿Parece una frase semilla, una clave privada WIF o una clave extendida privada?"""
    stripped = text.strip()
    if not stripped:
        return False
    # Frase semilla: 11 o más palabras (a veces numeradas o separadas por comas).
    # Una dirección nunca tiene espacios ni comas.
    if any(c.isspace() or c in ",;" for c in stripped):
        words = [w for w in re.split(r"[\W\d_]+", stripped) if w]
        if len(words) >= 11:
            return True
    if stripped[:4].lower() in ("xprv", "yprv", "zprv", "tprv", "uprv", "vprv"):
        return True
    # 32 bytes en hex (con o sin 0x, espacios o dos puntos): una clave privada en bruto.
    compact = re.sub(r"[\s:]", "", stripped)
    if compact[:2].lower() == "0x":
        compact = compact[2:]
    if len(compact) == 64 and all(c in _HEX_CHARS for c in compact):
        return True
    if 51 <= len(stripped) <= 52 and stripped[0] in "5KLc9":
        try:
            payload = base58check_decode(stripped)
        except AddressError:
            return False
        return payload[0] in (0x80, 0xEF) and len(payload) in (33, 34)
    return False


_SECRET_MESSAGE = (
    "Eso parece tu FRASE SEMILLA o una CLAVE PRIVADA. No la escribas nunca en "
    "ningún programa ni se la des a nadie: quien la tenga se queda tu dinero. "
    "El minero solo necesita tu dirección pública de recibir (empieza por bc1, 1 o 3)."
)


def _unwrap(text: str) -> str:
    """Quita comillas y el formato ``bitcoin:DIRECCION?amount=...`` de los QR."""
    address = text.strip().strip("\"'<>").strip()
    if address.lower().startswith("bitcoin:"):
        address = address[len("bitcoin:"):].split("?", 1)[0]
    return address.strip()


def validate_payout_address(text: str) -> tuple[str, bytes]:
    """Comprueba la dirección de cobro. Devuelve ``(dirección, scriptPubKey)``."""
    if looks_like_secret(text):
        raise SecretDetectedError(_SECRET_MESSAGE)
    address = _unwrap(text)
    if not address:
        raise AddressError("no has escrito ninguna dirección")
    if looks_like_secret(address):
        raise SecretDetectedError(_SECRET_MESSAGE)
    lowered = address.lower()
    if lowered.startswith(("lnbc", "lntb", "lnurl")) or "@" in address:
        raise AddressError("eso es de Lightning; el premio de un bloque necesita una dirección "
                           "normal (on-chain) que empiece por bc1, 1 o 3")
    if lowered.startswith("0x"):
        raise AddressError("eso es una dirección de Ethereum, no de Bitcoin")
    if address[0] in "48" and len(address) in (95, 106):
        raise AddressError("eso parece una dirección de Monero; este minero necesita una de "
                           "Bitcoin (para minar Monero mira MONERO.md)")
    script_pubkey = address_to_script_pubkey(address)
    if lowered.startswith("bc1"):
        address = lowered  # los QR usan mayúsculas; los pools y exploradores, minúsculas
    return address, script_pubkey
