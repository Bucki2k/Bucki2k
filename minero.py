#!/usr/bin/env python3
"""Minero de Bitcoin en solitario conectado a un solo pool.

Uso:
    python minero.py --wallet TU_DIRECCION_BTC
    python minero.py --benchmark          (mide tu velocidad sin conectarse)

Ejecuta ``python minero.py --help`` para ver todas las opciones.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import sys
from pathlib import Path

from minero_btc import __version__
from minero_btc.address import AddressError, SecretDetectedError, validate_payout_address
from minero_btc.miner import Miner, MinerConfig, log_line, run_benchmark
from minero_btc.stratum import parse_pool_url

DEFAULT_POOL = "public-pool.io:21496"
CONFIG_FILE = Path(__file__).resolve().parent / "minero_config.json"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minero de Bitcoin en solitario: si encuentras un bloque, la recompensa "
                    "va directa a tu dirección.",
    )
    parser.add_argument("-w", "--wallet", help="tu dirección PÚBLICA de Bitcoin (bc1..., 1... o 3...)")
    parser.add_argument("-o", "--pool", default=None,
                        help=f"pool en solitario como host:puerto (por defecto {DEFAULT_POOL}; "
                             "alternativa: solo.ckpool.org:3333)")
    parser.add_argument("--trabajador", default=None,
                        help="nombre de este equipo en el pool (por defecto, el nombre del PC)")
    parser.add_argument("--password", default="x", help="contraseña para el pool (casi siempre 'x')")
    parser.add_argument("-t", "--hilos", type=int, default=os.cpu_count() or 1,
                        help="procesos de minado (por defecto, todos los hilos de tu CPU)")
    parser.add_argument("--dificultad", type=float, default=1,
                        help="dificultad de share que se pide al pool (0 = no pedir ninguna)")
    parser.add_argument("--prioridad", choices=["baja", "normal"], default="baja",
                        help="'baja' deja el PC usable mientras mina (por defecto)")
    parser.add_argument("--intervalo", type=float, default=30,
                        help="segundos entre estadísticas (por defecto 30)")
    parser.add_argument("--benchmark", action="store_true",
                        help="medir la velocidad de tu CPU sin conectarse a ningún pool")
    parser.add_argument("--segundos", type=float, default=20, help="duración del benchmark")
    parser.add_argument("--no-guardar", action="store_true",
                        help="no guardar la dirección y el pool en minero_config.json")
    parser.add_argument("--version", action="version", version=f"minero-btc {__version__}")
    args = parser.parse_args(argv)
    if args.hilos < 1:
        parser.error("--hilos debe ser al menos 1")
    if args.dificultad < 0:
        parser.error("--dificultad no puede ser negativa")
    if args.intervalo < 1:
        parser.error("--intervalo debe ser al menos 1 segundo")
    return args


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"No se pudo guardar {CONFIG_FILE.name}: {exc}")


def ask_wallet() -> str:
    print("Escribe tu dirección PÚBLICA de Bitcoin para recibir la recompensa.")
    print("(La de 'Recibir' en tu wallet: empieza por bc1, 1 o 3. NUNCA tu frase semilla.)")
    return input("Dirección: ").strip()


def resolve_wallet(args, saved: dict) -> tuple[str, bytes]:
    candidate = args.wallet or saved.get("wallet")
    interactive = args.wallet is None
    while True:
        if candidate is None:
            if not sys.stdin.isatty():
                raise SystemExit("Falta la dirección: usa --wallet TU_DIRECCION")
            candidate = ask_wallet()
        try:
            return validate_payout_address(candidate)
        except SecretDetectedError as exc:
            print(f"\n*** {exc} ***\n")
        except AddressError as exc:
            print(f"Dirección no válida: {exc}")
        if not interactive:
            raise SystemExit(1)
        candidate = None


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.benchmark:
        run_benchmark(args.hilos, args.segundos, args.prioridad == "baja")
        return 0

    saved = load_config()
    address, script_pubkey = resolve_wallet(args, saved)
    pool_url = args.pool or saved.get("pool") or DEFAULT_POOL
    try:
        host, port, use_tls = parse_pool_url(pool_url)
    except ValueError as exc:
        print(f"Pool no válido: {exc}")
        return 1
    worker_name = args.trabajador or saved.get("trabajador") or _default_worker_name()

    if not args.no_guardar:
        new_config = {"wallet": address, "pool": pool_url, "trabajador": worker_name}
        if new_config != saved:
            save_config(new_config)

    config = MinerConfig(
        host=host, port=port, use_tls=use_tls,
        address=address, script_pubkey=script_pubkey,
        worker_name=worker_name, password=args.password,
        threads=args.hilos, suggest_difficulty=args.dificultad or None,
        stats_interval=args.intervalo, low_priority=args.prioridad == "baja",
    )
    log_line(f"minero-btc {__version__} | pool {host}:{port} | {args.hilos} procesos | "
             f"prioridad {args.prioridad}")
    log_line("Minería en solitario: es una lotería. Pulsa Ctrl+C para parar.")
    Miner(config).run()
    return 0


def _default_worker_name() -> str:
    name = "".join(c for c in socket.gethostname() if c.isalnum() or c in "-_")
    return name[:32] or "minero"


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
