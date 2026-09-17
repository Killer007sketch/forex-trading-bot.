"""Synthetic, broker-free deterministic tests of research trade state machine."""
import datetime as dt
import unittest

from aggressive_range_backtest_h1 import HORIZON, trading_cycle

BASE=dt.datetime(2026,1,5,tzinfo=dt.timezone.utc)
LO,HI=1.,1.01


def bar(j, o=1.005, h=1.006, l=1.004, c=1.005):
    return (BASE+dt.timedelta(hours=j),o,h,l,c)


def run(window, ratios=None, risk=.015, spread=1.5, slip=.3):
    ratios = ratios or [(2.,.0001)]*HORIZON
    return trading_cycle(window,ratios,LO,HI,10000.,10000.,0.,risk,spread,slip)


class Trades(unittest.TestCase):
    def test_no_trade_at_midpoint(self):
        bal,peak,dd,trades,events=run([bar(i) for i in range(12)])
        self.assertEqual((bal,len(trades)),(10000.,0))

    def test_stop_before_take_profit_on_both_touch(self):
        bars=[bar(0,o=1.002,h=1.008,l=.999,c=1.003)]+[bar(i) for i in range(1,12)]
        bal,peak,dd,trades,events=run(bars)
        self.assertEqual(trades[0]['exit_reason'],'stops')
        self.assertLess(bal,10000.)
        self.assertEqual(events['breach_hours'],1)

    def test_pause_return_requires_closed_inside_hour_and_ratio(self):
        bars=[bar(0,h=1.011,l=1.004)]+[bar(1,o=1.002,h=1.003,l=1.001,c=1.002),
                 bar(2,o=1.002,h=1.008,l=1.001,c=1.007)]+[bar(i) for i in range(3,12)]
        bal,peak,dd,trades,events=run(bars)
        self.assertEqual(events['pause_transitions'],1)
        self.assertEqual(events['resume_transitions'],1)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['entry_utc'],bars[2][0].isoformat())

    def test_atr_failure_blocks_next_open_until_rearm(self):
        bars=[bar(i) for i in range(12)]
        bars[1]=bar(1,o=1.002,h=1.003,l=1.001,c=1.002)
        bars[2]=bar(2,o=1.002,h=1.003,l=1.001,c=1.002)
        bars[3]=bar(3,o=1.002,h=1.008,l=1.001,c=1.007)
        ratios=[(6.,.0001),(6.,.0001),(2.,.0001)]+[(2.,.0001)]*9
        bal,peak,dd,trades,events=run(bars,ratios)
        self.assertEqual(events['ratio_pauses'],1)
        self.assertEqual(trades[0]['entry_utc'],bars[3][0].isoformat())

    def test_forced_expiry_no_future_bar(self):
        bars=[bar(0,o=1.002,h=1.003,l=1.001,c=1.002)]+[bar(i,o=1.004,h=1.005,l=1.003,c=1.004) for i in range(1,12)]
        bal,peak,dd,trades,events=run(bars)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['exit_reason'],'expiry')
        self.assertEqual(trades[0]['exit_utc'],(BASE+dt.timedelta(hours=12)).isoformat())

    def test_double_cost_worse_same_price_trade(self):
        bars=[bar(0,o=1.002,h=1.008,l=1.001,c=1.007)]+[bar(i) for i in range(1,12)]
        a=run(bars,spread=1.5,slip=.3)[3]
        b=run(bars,spread=3.,slip=.6)[3]
        self.assertEqual(a[0]['exit_reason'],b[0]['exit_reason'])
        self.assertGreater(a[0]['pnl_usd']/a[0]['units_eur'],b[0]['pnl_usd']/b[0]['units_eur'])


if __name__=='__main__':
    unittest.main()
