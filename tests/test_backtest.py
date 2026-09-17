import unittest
from src.backtest import run


class BacktestTests(unittest.TestCase):
    def test_flat_market(self):
        result, trades = run([(1.1, 1.101, 1.099, 1.1)] * 70)
        self.assertEqual(result['trades'], 0)
        self.assertEqual(result['final_usd'], 300)

    def test_trend(self):
        bars = []
        for i in range(100):
            p = 1.1 + i * .0002
            bars.append((p, p + .00015, p - .00015, p + .0001))
        result, trades = run(bars)
        self.assertGreaterEqual(result['trades'], 1)
        self.assertEqual(result['bars'], 100)


if __name__ == '__main__':
    unittest.main()
