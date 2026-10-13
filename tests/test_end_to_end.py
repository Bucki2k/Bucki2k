"""Prueba completa contra un pool Stratum falso en 127.0.0.1.

Usa datos inventados y dificultades minúsculas: no mina nada real ni se
conecta a Internet.
"""

import threading
import time
import unittest

from mock_pool import MockPool, make_coinbase_parts

from minero_btc.address import address_to_script_pubkey
from minero_btc.miner import Miner, MinerConfig
from minero_btc.stratum import StratumClient, StratumError

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
SCRIPT = address_to_script_pubkey(ADDRESS)
OTHER_SCRIPT = address_to_script_pubkey("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
EASY_DIFFICULTY = 2 ** -12  # una share cada ~1 millón de hashes


def wait_for(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class MinerRun:
    """Ejecuta el minero en un hilo y guarda su salida."""

    def __init__(self, pool: MockPool, threads: int = 2):
        self.lines: list[str] = []
        self.stop = threading.Event()
        config = MinerConfig(host="127.0.0.1", port=pool.port, use_tls=False, address=ADDRESS,
                             script_pubkey=SCRIPT, worker_name="test", threads=threads,
                             suggest_difficulty=1, stats_interval=2, low_priority=False)
        self.miner = Miner(config, log=self.lines.append, stop_event=self.stop)
        self.miner.reconnect_delay = 0.5
        self.thread = threading.Thread(target=self.miner.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "el minero no se detuvo"

    def output(self) -> str:
        return "\n".join(self.lines)


class EndToEndTests(unittest.TestCase):
    def make_pool(self, **kwargs) -> MockPool:
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 312_500_000, 4 + 4)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=EASY_DIFFICULTY, **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_mines_valid_shares_and_checks_payout(self):
        pool = self.make_pool()
        with MinerRun(pool) as run:
            self.assertTrue(wait_for(lambda: pool.valid_shares() >= 4, 90), run.output())
            self.assertTrue(wait_for(lambda: run.miner.accepted >= 4, 10), run.output())
        invalid = [s for s in pool.submissions if not s["valid"]]
        self.assertEqual(invalid, [], run.output())
        self.assertEqual(run.miner.rejected, 0)
        output = run.output()
        self.assertIn("van DIRECTAMENTE a tu dirección", output)
        self.assertIn("100.0 %", output)
        # Parámetros de mining.submit como los espera un pool real.
        user, job_id, extranonce2, ntime, nonce = pool.submissions[0]["params"]
        self.assertEqual(user, f"{ADDRESS}.test")
        self.assertEqual(job_id, "job1")
        self.assertEqual(len(extranonce2), 8)
        self.assertEqual(ntime, pool.ntime)
        self.assertEqual(len(nonce), 8)
        # suggest_difficulty se envía como entero (ckpool ignora los decimales)
        suggest = [r for r in pool.requests if r["method"] == "mining.suggest_difficulty"]
        self.assertEqual(suggest[0]["params"], [1])
        # Contestamos al ping del pool.
        self.assertIn({"id": 9001, "result": "pong", "error": None}, pool.replies)

    def test_reconnects_after_disconnect(self):
        pool = self.make_pool(disconnect_after_shares=1)
        with MinerRun(pool) as run:
            self.assertTrue(wait_for(lambda: pool.connections >= 3, 90), run.output())
            self.assertTrue(wait_for(lambda: pool.valid_shares() >= 2, 60), run.output())
        self.assertIn("Reconectando", run.output())

    def test_warns_when_pool_does_not_pay_you(self):
        coinb1, coinb2 = make_coinbase_parts(OTHER_SCRIPT, 312_500_000, 4 + 4)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1e6)
        self.addCleanup(pool.close)
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: "NO paga a tu dirección" in run.output(), 30), run.output())

    def test_reports_pool_fee(self):
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 306_250_000, 4 + 4,
                                             fee_script=OTHER_SCRIPT, fee_sats=6_250_000)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1e6)
        self.addCleanup(pool.close)
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: "comisión del pool" in run.output(), 30), run.output())
        self.assertIn("98.0 %", run.output())

    def test_block_found_is_reported(self):
        # nbits de regtest: cualquier share es también un "bloque".
        pool = self.make_pool(nbits="207fffff")
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: "El pool ACEPTÓ el bloque" in run.output(), 60), run.output())
        self.assertIn("BLOQUE ENCONTRADO", run.output())
        self.assertGreaterEqual(run.miner.blocks_found, 1)


class HandshakeTests(unittest.TestCase):
    def test_rejected_authorization(self):
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 1, 8)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1, authorize_result=False)
        self.addCleanup(pool.close)
        client = StratumClient("127.0.0.1", pool.port)
        client.connect()
        self.addCleanup(client.close)
        with self.assertRaises(StratumError):
            client.handshake(f"{ADDRESS}.x", "x")

    def test_subscribe_sets_extranonce(self):
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 1, 8)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1, extranonce1="abcd", extranonce2_size=6)
        self.addCleanup(pool.close)
        client = StratumClient("127.0.0.1", pool.port)
        client.connect()
        self.addCleanup(client.close)
        client.handshake(f"{ADDRESS}.x", "x")
        self.assertEqual(client.extranonce1, bytes.fromhex("abcd"))
        self.assertEqual(client.extranonce2_size, 6)
        kinds = [client.events.get(timeout=5)[0] for _ in range(2)]
        self.assertEqual(kinds, ["set_difficulty", "notify"])


if __name__ == "__main__":
    unittest.main()
