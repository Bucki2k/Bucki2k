import unittest

from minero_btc.consensus import (
    DIFF1_TARGET,
    bits_to_target,
    build_coinbase,
    build_header_prefix,
    bytes_to_stratum_prevhash,
    coinbase_payout,
    difficulty_to_target,
    hash_to_hex,
    merkle_root,
    nonce_to_hex,
    parse_coinbase_outputs,
    sha256d,
    stratum_prevhash_to_bytes,
    target_to_difficulty,
)
from minero_btc.worker import scan

# Bloque #1 de Bitcoin (9 de enero de 2009): solo tiene la transacción coinbase.
BLOCK1_HASH = "00000000839a8e6886ab5951d76f411475428afc90947ee320161bbf18eb6048"
BLOCK1_PREV = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
BLOCK1_MERKLE = "0e3e2357e806b6cdb1f70b54c3a3a17b6714ee1f0e68bebb44a74b1efd512098"
BLOCK1_TIME = 1231469665
BLOCK1_NONCE = 2573394689
BLOCK1_COINBASE = bytes.fromhex(
    "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff07"
    "04ffff001d0104ffffffff0100f2052a0100000043410496b538e853519c726a2c91e61ec11600ae1390"
    "813a627c66fb8be7947be63c52da7589379515d4e0a604f8141781e62294721166bf621e73a82cbf2342"
    "c858eeac00000000"
)
GENESIS_HEADER = bytes.fromhex(
    "0100000000000000000000000000000000000000000000000000000000000000000000003ba3edfd7a7b12b2"
    "7ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a29ab5f49ffff001d1dac2b7c"
)


def block1_as_stratum_job():
    """El bloque #1 tal como lo enviaría un pool por Stratum."""
    split = BLOCK1_COINBASE.index(bytes.fromhex("04ffff001d0104"))
    coinb1 = BLOCK1_COINBASE[:split + 4]
    extranonce1 = BLOCK1_COINBASE[split + 4:split + 6]
    extranonce2 = BLOCK1_COINBASE[split + 6:split + 7]
    coinb2 = BLOCK1_COINBASE[split + 7:]
    prevhash = bytes_to_stratum_prevhash(bytes.fromhex(BLOCK1_PREV)[::-1])
    return coinb1, extranonce1, extranonce2, coinb2, prevhash


class HashingTests(unittest.TestCase):
    def test_genesis_block_hash(self):
        self.assertEqual(hash_to_hex(sha256d(GENESIS_HEADER)), BLOCK1_PREV)

    def test_block1_from_stratum_job(self):
        coinb1, en1, en2, coinb2, prevhash = block1_as_stratum_job()
        coinbase = build_coinbase(coinb1, en1, en2, coinb2)
        self.assertEqual(coinbase, BLOCK1_COINBASE)
        root = merkle_root(coinbase, [])
        self.assertEqual(hash_to_hex(root), BLOCK1_MERKLE)
        prefix = build_header_prefix("00000001", prevhash, root, f"{BLOCK1_TIME:08x}", "1d00ffff")
        header = prefix + BLOCK1_NONCE.to_bytes(4, "little")
        self.assertEqual(len(header), 80)
        self.assertEqual(hash_to_hex(sha256d(header)), BLOCK1_HASH)

    def test_scan_finds_real_block1_nonce(self):
        coinb1, en1, en2, coinb2, prevhash = block1_as_stratum_job()
        root = merkle_root(build_coinbase(coinb1, en1, en2, coinb2), [])
        prefix = build_header_prefix("00000001", prevhash, root, f"{BLOCK1_TIME:08x}", "1d00ffff")
        found = scan(prefix, BLOCK1_NONCE - 20_000, BLOCK1_NONCE + 20_000, bits_to_target(0x1D00FFFF))
        self.assertEqual([(n, hash_to_hex(d)) for n, d in found], [(BLOCK1_NONCE, BLOCK1_HASH)])
        self.assertEqual(nonce_to_hex(BLOCK1_NONCE), "9962e301")

    def test_scan_matches_brute_force(self):
        prefix = bytes(range(76))
        digests = [sha256d(prefix + nonce.to_bytes(4, "little")) for nonce in range(3000)]
        # Camino rápido (prefiltro del byte alto), su límite exacto y el camino genérico.
        for target in (DIFF1_TARGET << 22, (1 << 248) - 1, 1 << 248, 1 << 250):
            with self.subTest(target=hex(target)):
                expected = [(nonce, digest) for nonce, digest in enumerate(digests)
                            if int.from_bytes(digest, "little") <= target]
                self.assertTrue(expected)
                self.assertEqual(scan(prefix, 0, 3000, target), expected)

    def test_prevhash_word_swap_roundtrip(self):
        raw = bytes(range(32))
        self.assertEqual(stratum_prevhash_to_bytes(bytes_to_stratum_prevhash(raw)), raw)
        self.assertEqual(stratum_prevhash_to_bytes("00010203" + "00" * 28)[:4], bytes([3, 2, 1, 0]))
        with self.assertRaises(ValueError):
            stratum_prevhash_to_bytes("00" * 31)

    def test_merkle_branches(self):
        a, b = sha256d(b"a"), sha256d(b"b")
        coinbase = b"coinbase"
        self.assertEqual(merkle_root(coinbase, [a, b]), sha256d(sha256d(sha256d(coinbase) + a) + b))


class TargetTests(unittest.TestCase):
    def test_bits(self):
        self.assertEqual(bits_to_target(0x1D00FFFF), DIFF1_TARGET)
        self.assertEqual(bits_to_target(0x1B0404CB), 0x0404CB << (8 * (0x1B - 3)))
        self.assertEqual(bits_to_target(0x03123456), 0x123456)
        self.assertEqual(bits_to_target(0x02123456), 0x1234)
        with self.assertRaises(ValueError):
            bits_to_target(0x1D800000)

    def test_difficulty_roundtrip(self):
        self.assertEqual(difficulty_to_target(1), DIFF1_TARGET)
        self.assertEqual(difficulty_to_target(2), DIFF1_TARGET // 2)
        self.assertAlmostEqual(target_to_difficulty(difficulty_to_target(1234.5)), 1234.5, places=6)
        self.assertEqual(difficulty_to_target(1e-80), (1 << 256) - 1)
        self.assertAlmostEqual(target_to_difficulty(bits_to_target(0x1B0404CB)), 16307.420938523983)
        with self.assertRaises(ValueError):
            difficulty_to_target(0)


class CoinbaseTests(unittest.TestCase):
    def test_parse_block1_coinbase(self):
        outputs = parse_coinbase_outputs(BLOCK1_COINBASE)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0][0], 50 * 100_000_000)
        self.assertEqual(coinbase_payout(BLOCK1_COINBASE, outputs[0][1]), (5_000_000_000, 5_000_000_000))
        self.assertEqual(coinbase_payout(BLOCK1_COINBASE, b"\x00\x14" + bytes(20)), (0, 5_000_000_000))

    def test_truncated_coinbase_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_coinbase_outputs(BLOCK1_COINBASE[:-10])
        with self.assertRaises(ValueError):
            parse_coinbase_outputs(BLOCK1_COINBASE + b"\x00")

    def test_segwit_serialization(self):
        # Misma coinbase con marcador/bandera segwit y un testigo de 32 bytes.
        body = BLOCK1_COINBASE[4:-4]
        tx = BLOCK1_COINBASE[:4] + b"\x00\x01" + body + b"\x01\x20" + bytes(32) + BLOCK1_COINBASE[-4:]
        self.assertEqual(parse_coinbase_outputs(tx), parse_coinbase_outputs(BLOCK1_COINBASE))


if __name__ == "__main__":
    unittest.main()
