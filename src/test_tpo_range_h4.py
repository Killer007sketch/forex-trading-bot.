"""Deterministic smoke and causality tests; NOT a market-accuracy validation."""
import datetime as dt
import math
import unittest

from tpo_range_h4 import H4, HOUR, build, outcomes, profile
from trend_h4 import indicators

UTC = dt.timezone.utc


def sample(n=235):
    start = dt.datetime(2020, 1, 1, tzinfo=UTC)
    h1, h4 = [], []
    for i in range(n):
        four = []
        for j in range(4):
            t = start + i*H4 + j*HOUR
            o = 1.10 + .0004*math.sin((4*i+j)/11)
            c = 1.10 + .0004*math.sin((4*i+j+1)/11)
            bar = (t, o, max(o,c)+.00006, min(o,c)-.00006, c)
            h1.append(bar); four.append(bar)
        h4.append((four[0][0],four[0][1], max(x[2] for x in four),
                   min(x[3] for x in four),four[-1][4]))
    return h1,h4


class ProfileTests(unittest.TestCase):
    def test_poc_midpoint_then_lower_tie(self):
        t=dt.datetime(2020,1,1,tzinfo=UTC)
        candle=(t, 1.0000, 1.0006, 1.0000, 1.0002)
        result=profile([candle], coverage=.7)
        self.assertAlmostEqual(result['poc'],1.0002)
        self.assertGreaterEqual(result['actual_coverage'],.70)
        self.assertLessEqual(result['val'],result['poc'])
        self.assertGreater(result['vah'],result['poc'])

    def test_malformed_parameters(self):
        with self.assertRaises(ValueError): profile([],coverage=.7)
        with self.assertRaises(ValueError): profile([(0,1,1,1,1)],coverage=1.01)

    def test_prefix_invariance(self):
        h1,h4=sample()
        atr=indicators(h4)[2]
        full,labels=build(h1,h4,atr)
        short,short_labels=build(h1[:4*232],h4[:232],atr[:232])
        self.assertEqual(full[:232],short)
        self.assertEqual(labels[:232],short_labels)
        self.assertIsNotNone(full[220])
        self.assertEqual(full[220]['available_utc'],(h4[220][0]+H4).isoformat())

    def test_no_retroactive_entry_and_boundary_break(self):
        h1,h4=sample()
        atr=indicators(h4)[2]
        snapshots,labels=build(h1,h4,atr)
        labels=['transition']*len(h4)
        labels[221]='range'
        p=snapshots[221].copy()
        p['outer_low']=1.0998;p['outer_high']=1.1002
        snapshots[221]=p
        statistics,events=outcomes(h1,h4,snapshots,labels,220,len(h4))
        self.assertEqual(statistics['nonoverlap_events'],1)
        self.assertEqual(events[0]['label_at_h4_close_utc'],(h4[221][0]+H4).isoformat())
        self.assertEqual(events[0]['wick_breach_24h'],1)


if __name__=='__main__': unittest.main()
