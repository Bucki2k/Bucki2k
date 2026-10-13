"""Cuentas honestas: cuánto tardarías de media en encontrar un bloque."""

from __future__ import annotations

import math

SECONDS_PER_YEAR = 365.25 * 24 * 3600
HASHES_PER_DIFF1 = 1 << 32  # hashes esperados para una share de dificultad 1


def expected_seconds_to_block(hashrate: float, network_difficulty: float) -> float:
    if hashrate <= 0:
        return math.inf
    return network_difficulty * HASHES_PER_DIFF1 / hashrate


def probability_within(seconds: float, hashrate: float, network_difficulty: float) -> float:
    """Probabilidad de encontrar al menos un bloque en ``seconds`` (proceso de Poisson)."""
    if hashrate <= 0:
        return 0.0
    expected_blocks = hashrate * seconds / (network_difficulty * HASHES_PER_DIFF1)
    return -math.expm1(-expected_blocks)


def format_hashrate(hashrate: float) -> str:
    units = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s", "EH/s", "ZH/s"]
    value = float(hashrate)
    for unit in units:
        if abs(value) < 1000 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1000
    raise AssertionError("inalcanzable")


def format_number(value: float) -> str:
    """Número grande en español (escala larga): 12.345, 3,2 millones, 4,1 billones."""
    for size, name in ((1e18, "trillones"), (1e12, "billones"), (1e9, "mil millones"), (1e6, "millones")):
        if value >= size * 0.9995:  # 999.999.999,9 se muestra como "1,0 mil millones"
            return f"{value / size:.1f} {name}".replace(".", ",")
    return f"{value:,.0f}".replace(",", ".")


def format_difficulty(difficulty: float) -> str:
    for size, suffix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if difficulty >= size:
            return f"{difficulty / size:.2f} {suffix}"
    if difficulty >= 1:
        return f"{difficulty:.2f}"
    return f"{difficulty:.3g}"


def format_duration(seconds: float) -> str:
    if math.isinf(seconds):
        return "nunca"
    if seconds < 120:
        return f"{seconds:.0f} segundos"
    if seconds < 7200:
        return f"{seconds / 60:.0f} minutos"
    if seconds < 3 * 86400:
        return f"{seconds / 3600:.1f} horas"
    if seconds < 2 * SECONDS_PER_YEAR:
        return f"{seconds / 86400:.0f} días"
    years = format_number(seconds / SECONDS_PER_YEAR)
    return f"{years} de años" if years[-1].isalpha() else f"{years} años"


def format_odds(probability: float) -> str:
    if probability <= 0:
        return "0"
    if probability >= 0.5:
        return f"{probability * 100:.1f} %"
    return f"1 entre {format_number(1 / probability)}"
