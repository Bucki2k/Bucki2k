import math
import unittest

from minero_btc import stats


class StatsTests(unittest.TestCase):
    def test_expected_time(self):
        # Dificultad 1 a 2^32 H/s: un bloque por segundo de media.
        self.assertAlmostEqual(stats.expected_seconds_to_block(2 ** 32, 1), 1.0)
        self.assertEqual(stats.expected_seconds_to_block(0, 1), math.inf)

    def test_probability(self):
        self.assertAlmostEqual(stats.probability_within(1, 2 ** 32, 1), 1 - math.exp(-1))
        tiny = stats.probability_within(stats.SECONDS_PER_YEAR, 10e6, 132.7e12)
        self.assertGreater(tiny, 0)
        self.assertLess(tiny, 1e-9)

    def test_formatting(self):
        self.assertEqual(stats.format_hashrate(12_340_000), "12.34 MH/s")
        self.assertEqual(stats.format_hashrate(950e18), "950.00 EH/s")
        self.assertEqual(stats.format_number(12345), "12.345")
        self.assertEqual(stats.format_number(3.2e6), "3,2 millones")
        self.assertEqual(stats.format_number(4.1e12), "4,1 billones")
        self.assertEqual(stats.format_duration(30), "30 segundos")
        self.assertEqual(stats.format_duration(3 * stats.SECONDS_PER_YEAR * 1e6), "3,0 millones de años")
        self.assertEqual(stats.format_odds(1e-9), "1 entre 1,0 mil millones")
        self.assertEqual(stats.format_difficulty(132.7e12), "132.70 T")


if __name__ == "__main__":
    unittest.main()
