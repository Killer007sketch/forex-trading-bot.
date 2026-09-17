"""First diagnostic test of causal H4 trend/range/transition labels; no trading."""
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path
from trend_h4 import load_h4, indicators


def classify(bars):
    fast, slow, atr, adx = indicators(bars)
    labels = ['warmup'] * len(bars)
    raw = ['transition'] * len(bars)
    closes = [b[4] for b in bars]
    for i in range(220, len(bars)):
        a = atr[i]
        if not a or not adx[i]:
            continue
        window = closes[i-19:i+1]
        path = sum(abs(closes[j]-closes[j-1]) for j in range(i-19,i+1))
        er = abs(closes[i]-closes[i-20])/path if path else 0
        prior_high = max(b[2] for b in bars[i-20:i])
        prior_low = min(b[3] for b in bars[i-20:i])
        slope = (fast[i]-fast[i-8]) / (8*a)
        direction = 1 if slope > .12 and fast[i]>slow[i] else -1 if slope < -.12 and fast[i]<slow[i] else 0
        breakout = (direction==1 and closes[i]>prior_high) or (direction==-1 and closes[i]<prior_low)
        width = (max(window)-min(window))/a
        if adx[i]>25 and er>.35 and direction and breakout:
            raw[i]='trend'
        elif adx[i]<20 and er<.25 and abs(slope)<.12 and width<5:
            raw[i]='range'
        # Two completed H4 signals required; all other cases transition.
        labels[i] = raw[i] if raw[i]==raw[i-1] else 'transition'
    return labels


def evaluate(bars, labels, start, end):
    counts=Counter(labels[start:end]); switches=0; prev=None
    by_label={k:[] for k in ('trend','range','transition')}
    for i in range(start,min(end,len(bars)-6)):
        label=labels[i]
        if label=='warmup':continue
        if prev is not None and prev!=label:switches+=1
        prev=label
        # Future movement is used ONLY for evaluation, never classification.
        path=sum(abs(bars[j][4]-bars[j-1][4]) for j in range(i+1,i+7))
        displacement=abs(bars[i+6][4]-bars[i][4])
        by_label[label].append(displacement/path if path else 0)
    return {'counts':dict(counts),'switches':switches,'forward_6bar_efficiency_mean':{k:round(sum(v)/len(v),4) if v else None for k,v in by_label.items()},'forward_samples':{k:len(v) for k,v in by_label.items()}}


def main():
    path=Path(sys.argv[1]);bars=load_h4(path);labels=classify(bars);split=int(len(bars)*.7)
    report={'data_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'method':'ADX+Kaufman ER+EMA slope+20bar breakout/range width; two consecutive completed H4 labels; fixed exploratory thresholds','development':evaluate(bars,labels,220,split),'holdout':evaluate(bars,labels,split,len(bars))}
    Path('reports').mkdir(exist_ok=True)
    Path('reports/regime_h4.json').write_text(json.dumps(report,indent=2)+'\n')
    with Path('reports/regime_h4.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['h4_open_utc','regime_available_after_h4_close'])
        w.writerows((b[0].isoformat(),labels[i]) for i,b in enumerate(bars))
    print(json.dumps(report,indent=2))
    print('DIAGNOSTIC ONLY: forward efficiency is not ground-truth accuracy, not a profitability test; historical holdout previously viewed in trading research.')

if __name__=='__main__':main()
