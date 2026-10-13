import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import minero
from minero_btc.consensus import difficulty_to_target
from minero_btc.miner import describe_error
from minero_btc.stratum import parse_pool_url
from minero_btc.worker import MAX_REPORTS_PER_CHUNK, _mine_job


class ConfigTests(unittest.TestCase):
    def load(self, raw: bytes) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "minero_config.json"
            path.write_bytes(raw)
            with mock.patch.object(minero, "CONFIG_FILE", path), mock.patch("builtins.print"):
                return minero.load_config()

    def test_bom_is_accepted(self):
        data = json.dumps({"wallet": "bc1q", "pool": "solo.ckpool.org:3333"}).encode()
        self.assertEqual(self.load(b"\xef\xbb\xbf" + data)["pool"], "solo.ckpool.org:3333")

    def test_wrong_types_are_ignored(self):
        self.assertEqual(self.load(b"[]"), {})
        self.assertEqual(self.load(b"null"), {})
        self.assertEqual(self.load(b'{"wallet": 123, "pool": "x:1"}'), {"pool": "x:1"})
        self.assertEqual(self.load(b"{roto"), {})

    def test_worker_name_is_cleaned(self):
        self.assertEqual(minero._clean_worker_name("Jason.PC ñ"), "JasonPC")
        self.assertEqual(minero._clean_worker_name("..."), "minero")

    def test_difficulty_option_limits(self):
        with mock.patch("sys.stderr"):
            for bad in ("0.0001", "nan", "inf", "-1", "2e9"):
                with self.subTest(value=bad), self.assertRaises(SystemExit):
                    minero.parse_args(["--dificultad", bad])
        self.assertEqual(minero.parse_args(["--dificultad", "0"]).dificultad, 0)


class PoolUrlTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_pool_url("public-pool.io:3333"), ("public-pool.io", 3333, False))
        self.assertEqual(parse_pool_url("stratum+tcp://solo.ckpool.org:3333/"), ("solo.ckpool.org", 3333, False))
        self.assertEqual(parse_pool_url("stratum+ssl://public-pool.io:4333"), ("public-pool.io", 4333, True))

    def test_invalid(self):
        for url in ("public-pool.io", "public-pool..io:3333", "http://x:1", "x:0", "x:99999", ":3333"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_pool_url(url)

    def test_errors_are_explained(self):
        self.assertIn("sin Internet", describe_error(socket.gaierror(11001, "getaddrinfo failed"), "x"))
        self.assertIn("no responde", describe_error(socket.timeout("timed out"), "x"))
        self.assertIn("puerto", describe_error(ConnectionRefusedError(111, "refused"), "x"))


class WorkerTests(unittest.TestCase):
    def test_reports_are_capped_and_keep_the_best(self):
        class Collect(list):
            put = list.append

        class Flag:
            value = 0

        job_queue = mock.Mock()
        job_queue.get_nowait.return_value = "stop"  # parar tras el primer bloque de nonces
        results = Collect()
        job = {
            "seq": 1, "coinb1": b"\x01", "coinb2": b"\x02", "extranonce1": b"", "extranonce2_size": 4,
            "merkle_branch": [], "prevhash": "00" * 32, "version": "20000000",
            "nbits": "1d00ffff", "ntime": "66f00000",
            "share_target": (1 << 256) - 1,  # objetivo absurdo: todos los hashes valdrían
            "network_target": difficulty_to_target(1),
        }
        outcome = _mine_job(job, 0, 1, job_queue, results, [0], Flag(), {"extranonce2_counter": 0})
        self.assertEqual(outcome, "stop")
        self.assertEqual(len(results), MAX_REPORTS_PER_CHUNK)
        difficulties = [r["difficulty"] for r in results]
        self.assertEqual(difficulties, sorted(difficulties, reverse=True))

    def test_bad_difficulties_are_rejected(self):
        for bad in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                difficulty_to_target(bad)


if __name__ == "__main__":
    unittest.main()
