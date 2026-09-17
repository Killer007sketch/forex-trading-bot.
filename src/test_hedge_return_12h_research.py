"""Synthetic executable invariants for equal hedges; no historical optimisation."""
import datetime as dt
import unittest
from hedge_return_12h_research import cycle

START=dt.datetime(2026,1,5,tzinfo=dt.timezone.utc)
LOWER=1.09
UPPER=1.11
RATIOS=[(4.,.001)]*12


def candle(j,op=1.1,hi=1.101,lo=1.099,close=1.1):
    return START+dt.timedelta(hours=j),op,hi,lo,close


def make(changes):
    bars=[candle(i) for i in range(12)]
    bars[0]=candle(0,1.095,1.096,1.094,1.095)
    for j,data in changes.items():bars[j]=candle(j,*data)
    return bars


def simulate(bars,mode,spread=1.5,slip=.3):
    return cycle(bars,RATIOS,LOWER,UPPER,10000.,10000.,0.,.015,spread,slip,mode)

class HedgeResearchTests(unittest.TestCase):
    def test_recovery_parity_with_same_bid_and_costs(self):
        # Hour 1 breaches, hour 2 trades strictly below; hour 3 opens inside.
        bars=make({1:(1.094,1.095,1.089,1.089),
                   2:(1.088,1.089,1.087,1.088),
                   3:(1.09,1.092,1.09,1.091),
                   4:(1.091,1.105,1.091,1.104)})
        a=simulate(bars,'equal_hedge');b=simulate(bars,'stop_and_reenter')
        self.assertEqual(a[3]['hedges_opened'],1)
        self.assertEqual(a[3]['hedges_released'],1)
        self.assertEqual(b[3]['cash_stops'],1)
        self.assertEqual(b[3]['cash_reentries'],1)
        self.assertAlmostEqual(a[0],b[0],places=6)
        self.assertEqual(a[5][0]['reason'],'targets')

    def test_no_return_hedge_expires_two_leg_cost(self):
        bars=make({1:(1.094,1.095,1.089,1.089),
                   **{j:(1.085,1.086,1.084,1.085) for j in range(2,12)}})
        a=simulate(bars,'equal_hedge');b=simulate(bars,'stop_and_reenter')
        self.assertEqual(a[3]['hedges_expired'],1)
        self.assertEqual(b[3]['cash_expired_unreturned'],1)
        self.assertLess(a[0],b[0])
        self.assertAlmostEqual(b[0]-a[0],a[5][0]['units_eur']*(1.5+2*.3)*.0001,places=6)

    def test_boundary_precedes_target_same_hour(self):
        bars=make({1:(1.095,1.106,1.088,1.095)})
        r=simulate(bars,'equal_hedge')
        self.assertEqual(r[3]['hedges_opened'],1)
        self.assertEqual(r[3]['targets'],0)

    def test_multiple_return_hedge_cycles_possible(self):
        bars=make({1:(1.094,1.095,1.089,1.089),
                   2:(1.089,1.091,1.088,1.09),
                   3:(1.091,1.092,1.088,1.089),
                   4:(1.089,1.092,1.088,1.091),
                   5:(1.092,1.105,1.091,1.104)})
        r=simulate(bars,'equal_hedge')
        self.assertGreaterEqual(r[3]['hedges_opened'],2)
        self.assertGreaterEqual(r[3]['ambiguous_return_and_rebreach_same_h1'],1)

    def test_gap_at_boundary_fills_worse_than_exact_level(self):
        bars=make({1:(1.085,1.086,1.084,1.085),
                   **{j:(1.084,1.085,1.083,1.084) for j in range(2,12)}})
        r=simulate(bars,'equal_hedge')
        self.assertEqual(r[3]['gap_adverse_breaches'],1)
        fill=[e for e in r[4] if e['action']=='open_equal_hedge'][0]
        self.assertAlmostEqual(fill['bid_reference'],1.085)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):simulate(make({}),'magic')

if __name__=='__main__':unittest.main()
