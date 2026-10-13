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
                             suggest_difficulty=1, min_share_difficulty=EASY_DIFFICULTY,
                             stats_interval=2, low_priority=False, prevent_sleep=False)
        self.miner = Miner(config, log=self.lines.append, stop_event=self.stop)
        self.miner.reconnect_delay = 0.5
        self.miner.min_network_difficulty = 0  # el pool falso usa dificultades de juguete
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
        # Dificultad de red de juguete (2^-10): uno de cada 4 shares es un "bloque".
        pool = self.make_pool(nbits="1e03fffc")
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: "El pool ACEPTÓ el bloque" in run.output(), 60), run.output())
        self.assertIn("BLOQUE ENCONTRADO", run.output())
        self.assertGreaterEqual(run.miner.blocks_found, 1)


class HostilePoolTests(unittest.TestCase):
    """Un pool malicioso o roto no debe poder tumbar el minero ni engañar al usuario."""

    def test_survives_malformed_messages(self):
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 312_500_000, 4 + 4)
        bad_job = ('{"id":null,"method":"mining.notify","params":["bad","%s","%s","%s",[],'
                   '"%s","%s","%s",false]}')
        raw = (
            '{"id":null,"method":"mining.set_difficulty","params":[1e400]}',
            '{"id":null,"method":"mining.set_difficulty","params":["inf"]}',
            '{"id":null,"method":"mining.set_difficulty","params":{"a":1}}',
            '{"id":null,"method":"mining.set_extranonce","params":7}',
            '{"id":null,"method":"mining.set_extranonce","params":["zz",4]}',
            '{"id":[1],"result":true,"error":null}',
            '{"id":null,"method":"client.show_message","params":["hola\\u001b[2A\\r\\ud800"]}',
            '{"id":null,"method":"mining.notify","params":{"a":1}}',
            bad_job % ("00" * 32, coinb1, coinb2, "20000000", "00000000", "66f00000"),
            bad_job % ("00" * 32, coinb1, coinb2, "20 00 00", "1d00ffff", "66f00000"),
            bad_job % ("00 00 " + "00" * 29, coinb1, coinb2, "20000000", "1d00ffff", "66f00000"),
            bad_job % ("00" * 32, coinb1, coinb2, "20000000", "207fffff", "66f00000"),
            "esto no es json",
            "[" * 50_000,
        )
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=EASY_DIFFICULTY,
                        raw_after_authorize=raw)
        self.addCleanup(pool.close)
        run = MinerRun(pool)
        run.miner.min_network_difficulty = 1e-9  # rechaza 207fffff (2^-31), acepta 1d00ffff (1)
        with run:
            self.assertTrue(wait_for(lambda: pool.valid_shares() >= 3, 90), run.output())
            self.assertEqual(run.miner.pool.alive(), 2, run.output())
        output = run.output()
        self.assertEqual(pool.connections, 1, output)  # ni se cayó ni entró en bucle de reconexión
        self.assertIn("Dificultad no válida del pool ignorada", output)
        self.assertIn("set_extranonce no válido", output)
        self.assertIn("Trabajo no válido", output)
        self.assertIn("dificultad de red imposible", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)
        self.assertEqual(run.miner.blocks_found, 0)

    def test_warns_when_pool_cuts_your_share_mid_session(self):
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 312_500_000, 4 + 4)
        _, stolen = make_coinbase_parts(SCRIPT, 1, 4 + 4, fee_script=OTHER_SCRIPT, fee_sats=312_499_999)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1e6,
                        followup_coinb2=stolen, followup_delay=1.0)
        self.addCleanup(pool.close)
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: "solo te pagaría el 0.0 %" in run.output(), 30), run.output())
        self.assertIn("100.0 %", run.output())

    def test_block_is_submitted_even_above_share_difficulty(self):
        # El pool pide shares de dificultad 1e6, pero la red (de juguete) es mucho más fácil:
        # cualquier bloque debe enviarse igualmente.
        coinb1, coinb2 = make_coinbase_parts(SCRIPT, 312_500_000, 4 + 4)
        pool = MockPool(coinb1=coinb1, coinb2=coinb2, difficulty=1e6, nbits="1e03fffc")
        self.addCleanup(pool.close)
        with MinerRun(pool, threads=1) as run:
            self.assertTrue(wait_for(lambda: len(pool.blocks) >= 1, 60), run.output())
            self.assertTrue(wait_for(lambda: "El pool ACEPTÓ el bloque" in run.output(), 10), run.output())


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
