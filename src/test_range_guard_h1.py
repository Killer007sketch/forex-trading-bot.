"""Small deterministic execution and state tests; historical CI is separate."""
import datetime as dt
import unittest
from range_guard_h1 import barrier, execution, simulate, units_for


class GuardTests(unittest.TestCase):
    def test_bid_ask_and_slippage(self):
        self.assertAlmostEqual(execution(1.1, 1, 1.5, .3), 1.10018)
        self.assertAlmostEqual(execution(1.1, -1, 1.5, .3), 1.09997)

    def test_both_edges_adverse_first_and_gap(self):
        self.assertEqual(barrier(1.10, 1.13, 1.08, 1.09, 1.12, 1), (1.09, 'lower'))
        self.assertEqual(barrier(1.10, 1.13, 1.08, 1.09, 1.12, -1), (1.12, 'upper'))
        self.assertEqual(barrier(1.07, 1.08, 1.06, 1.09, 1.12, 1), (1.07, 'lower'))
        self.assertEqual(barrier(1.14, 1.15, 1.13, 1.09, 1.12, -1), (1.14, 'upper'))

    def test_sizing_respects_minimum_lot_and_risk(self):
        size = units_for(1000, 1.101, 1, 1.0995, 1.1105, 1.5, .3)
        self.assertEqual(size % 1000, 0)
        self.assertLessEqual(size * 1.10118, 5000)
        self.assertLessEqual(size * (1.10118 - 1.09947), 5.00001)
        self.assertEqual(units_for(10, 1.101, 1, 1.0995, 1.1105, 1.5, .3), 0)

    def test_guard_disarms_without_new_nonrange(self):
        start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        h4 = [(start+dt.timedelta(hours=4*i), 1.105, 1.11, 1.10, 1.105) for i in range(225)]
        h1 = []
        for i in range(225*4):
            t = start + dt.timedelta(hours=i)
            op, hi, lo = 1.105, 1.1055, 1.1045
            if i == 222*4:
                op, hi, lo = 1.101, 1.1013, 1.1007
            if i == 222*4+1:
                op, hi, lo = 1.1008, 1.101, 1.0995
            h1.append((t, op, hi, lo, op))
        labels = ['transition']*225
        for j in range(220,225):
            labels[j] = 'range'
        result, trades = simulate(h1,h4,labels,[6]*225,[60]*225,[.002]*225,220,225,1.5,.3,True,True)
        self.assertEqual(result['events']['cycles_started'], 1)
        self.assertEqual(result['events']['boundary_shutdown'], 1)
        self.assertEqual(result['trades'], 1)
        self.assertEqual(trades[0][5], 'h1_guard')
        self.assertLess(result['final_usd'], 1000)


if __name__ == '__main__':
    unittest.main()
