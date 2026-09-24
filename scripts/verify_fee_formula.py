#!/usr/bin/env python3
'''
Verify fee formula — independent recomputation check for core/fees.py.

Recomputes the Kalshi taker fee ceil(0.07 x C x P x (1-P)) cents per side
directly with math.ceil (an independent path through the formula) and
compares it against core.fees.kalshi_fee() on a grid of (contracts, price)
cases. Cron-safe: short output, explicit exit code (0 = verified).

Run standalone or via the fee-verify cron; prints nothing on success.
'''
import math
import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


from core.fees import kalshi_fee

def verify():
    expected = lambda contracts, price: math.ceil(0.07 * price * (1.0 - price) * contracts) / 100.0
    mismatches = []


    for contracts, price in [(1, 0.50), (10, 0.50), (100, 0.50), (100, 0.10), (1000, 0.99)]:
        got = kalshi_fee(contracts, price)
        want = expected(contracts, price)
        if got != want:
            mismatches.append(f'kalshi_fee({contracts}, {price}) = {got}, expected {want}')


    return '\n'.join(mismatches)


if __name__ == '__main__':
    result = verify()
    if result:
        print(result)
        sys.exit(1)
    print('NO_REPLY')
    sys.exit(0)