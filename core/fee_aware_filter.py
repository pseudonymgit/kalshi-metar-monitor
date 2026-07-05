#!/usr/bin/env python3
"""
P5: Fee-Aware Entry Filter + Minimum Price Floor

Entry rules that reject trades when:
1. Dollar edge after fees/spread < minimum threshold
2. Price below minimum floor (penny-contract trap)

Kalshi fee structure: ~5% on profits (varies by market tier)
Spread: bid-ask spread must be accounted for

Usage:
    from core.fee_aware_filter import FeeAwareEntryFilter
    faef = FeeAwareEntryFilter()
    decision = faef.evaluate_entry(
        model_prob=0.65, market_price=0.55, stake=10.0
    )
    # Returns: {'approve': bool, 'edge_after_fees': float, 'reason': str}
"""

# ─── Configuration ──────────────────────────────────────────────────────────

# Kalshi fee structure (approximate)
FEE_RATE = 0.05          # 5% on profits (standard Kalshi contract)
SPREAD_ASSUMPTION = 0.02  # 2 cents assumed bid-ask spread for liquid markets

# Entry thresholds
MIN_DOLLAR_EDGE = 0.50   # Minimum dollar profit after fees/spread to enter
MIN_PRICE_FLOOR = 0.05  # Don't buy contracts below 5 cents (penny-contract trap)
MAX_PRICE_CEILING = 0.95  # Don't buy contracts above 95 cents (symmetric trap)

# Position sizing
DEFAULT_STAKE = 10.0    # Default stake per trade in dollars


class FeeAwareEntryFilter:
    """
    Pre-trade filter that rejects entries with insufficient edge after fees
    or with prices in the penny-contract danger zone.
    """

    def __init__(self, fee_rate=FEE_RATE, spread=SPREAD_ASSUMPTION,
                 min_edge=MIN_DOLLAR_EDGE, min_price=MIN_PRICE_FLOOR,
                 max_price=MAX_PRICE_CEILING):
        self.fee_rate = fee_rate
        self.spread = spread
        self.min_edge = min_edge
        self.min_price = min_price
        self.max_price = max_price

    def evaluate_entry(self, model_prob, market_price, stake=DEFAULT_STAKE,
                       direction='up'):
        """
        Evaluate whether a trade should be entered based on fee-adjusted edge.

        Args:
            model_prob: Model's probability of the event (0.0-1.0)
            market_price: Current market price for the YES contract (0.0-1.0)
            stake: Dollar amount to stake
            direction: 'up' or 'down' (for logging)

        Returns:
            dict with:
                approve: bool
                edge_after_fees: float (dollar edge)
                expected_profit: float
                expected_loss: float
                fee_amount: float
                reason: str (rejection reason if not approved)
        """
        # ─── Price floor / ceiling check ───
        if market_price < self.min_price:
            return self._reject(
                f"Price {market_price:.3f} below floor {self.min_price:.3f} "
                f"(penny-contract trap)",
                model_prob, market_price, stake)

        if market_price > self.max_price:
            return self._reject(
                f"Price {market_price:.3f} above ceiling {self.max_price:.3f} "
                f"(inverse penny-contract trap)",
                model_prob, market_price, stake)

        # ─── Edge calculation ───
        # Expected profit if we win: (1 - market_price) * stake - fees
        # Expected loss if we lose: market_price * stake
        # Edge = model_prob * expected_profit - (1 - model_prob) * expected_loss

        gross_profit = (1 - market_price) * stake
        fee_amount = gross_profit * self.fee_rate
        net_profit = gross_profit - fee_amount - (self.spread * stake)
        net_loss = market_price * stake + (self.spread * stake)

        expected_value = model_prob * net_profit - (1 - model_prob) * net_loss

        if expected_value < 0:
            return self._reject(
                f"Negative expected value: ${expected_value:.2f} "
                f"(model_prob={model_prob:.3f}, price={market_price:.3f})",
                model_prob, market_price, stake, ev=expected_value,
                fee=fee_amount, net_profit=net_profit, net_loss=net_loss)

        if expected_value < self.min_edge:
            return self._reject(
                f"Dollar edge ${expected_value:.2f} below minimum ${self.min_edge:.2f} "
                f"(after {self.fee_rate*100:.0f}% fees + {self.spread:.2f} spread)",
                model_prob, market_price, stake, ev=expected_value,
                fee=fee_amount, net_profit=net_profit, net_loss=net_loss)

        return {
            'approve': True,
            'edge_after_fees': expected_value,
            'expected_profit': net_profit,
            'expected_loss': net_loss,
            'fee_amount': fee_amount,
            'reason': f"Approved: edge ${expected_value:.2f} > ${self.min_edge:.2f} min",
            'model_prob': model_prob,
            'market_price': market_price,
            'stake': stake,
        }

    def evaluate_spread(self, bid, ask):
        """
        Evaluate whether the bid-ask spread is tradeable.

        Args:
            bid: Best bid price
            ask: Best ask price

        Returns: dict with approve, spread, spread_pct, reason
        """
        spread = ask - bid
        mid = (ask + bid) / 2
        spread_pct = spread / mid if mid > 0 else 1.0

        if spread > 0.10:
            return {'approve': False, 'spread': spread, 'spread_pct': spread_pct,
                    'reason': f"Spread too wide: {spread:.3f} ({spread_pct:.1%})"}
        if spread_pct > 0.15:
            return {'approve': False, 'spread': spread, 'spread_pct': spread_pct,
                    'reason': f"Spread % too high: {spread_pct:.1%}"}

        return {'approve': True, 'spread': spread, 'spread_pct': spread_pct,
                'reason': f"Spread acceptable: {spread:.3f} ({spread_pct:.1%})"}

    def batch_evaluate(self, signals):
        """
        Evaluate multiple signals at once.

        Args:
            signals: list of dicts with model_prob, market_price, stake, direction

        Returns: list of evaluation results (only approved entries)
        """
        approved = []
        for sig in signals:
            result = self.evaluate_entry(
                model_prob=sig['model_prob'],
                market_price=sig['market_price'],
                stake=sig.get('stake', DEFAULT_STAKE),
                direction=sig.get('direction', 'up')
            )
            if result['approve']:
                approved.append({**sig, **result})

        return approved

    def _reject(self, reason, model_prob, market_price, stake,
                ev=0, fee=0, net_profit=0, net_loss=0):
        return {
            'approve': False,
            'edge_after_fees': ev,
            'expected_profit': net_profit,
            'expected_loss': net_loss,
            'fee_amount': fee,
            'reason': reason,
            'model_prob': model_prob,
            'market_price': market_price,
            'stake': stake,
        }


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    faef = FeeAwareEntryFilter()

    print("=== Fee-Aware Entry Filter Tests ===\n")

    test_cases = [
        # (model_prob, market_price, stake, description)
        (0.65, 0.50, 10.0, "Good edge: 65% prob at 50¢"),
        (0.55, 0.50, 10.0, "Marginal edge: 55% prob at 50¢"),
        (0.52, 0.50, 10.0, "Tiny edge: 52% prob at 50¢"),
        (0.70, 0.03, 10.0, "Penny-contract trap: 70% prob at 3¢"),
        (0.70, 0.97, 10.0, "Ceiling trap: 70% prob at 97¢"),
        (0.60, 0.55, 10.0, "Moderate: 60% prob at 55¢"),
        (0.80, 0.70, 25.0, "Strong signal: 80% prob at 70¢, $25 stake"),
        (0.50, 0.50, 10.0, "No edge: 50% prob at 50¢"),
    ]

    print(f"{'Description':<45} {'Approve':>8} {'Edge':>8} {'Reason'}")
    print("-" * 100)

    for prob, price, stake, desc in test_cases:
        result = faef.evaluate_entry(prob, price, stake)
        appr = "YES" if result['approve'] else "NO"
        print(f"{desc:<45} {appr:>8} ${result['edge_after_fees']:>6.2f}  {result['reason']}")
