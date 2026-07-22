
# Deployment Plan Considerations

## Pre-Launch Checklist (Unattended Real Money Trading)
- [ ] Live API credentials tested in paper mode for >7 days
- [ ] Market hours monitoring for signal generation
- [ ] Emergency shut-off protocol tested
- [ ] Slippage and execution cost incorporated into simulations
- [ ] Minimum liquidity criteria established for each traded station

## Production Environment Configs
- SBOX (Sandbox): Initial launch with reduced position sizes
- PROD: Gradual scale-up after SBOX validation

## Monitoring Alerts
- Daily accuracy < 55% triggers alert
- Max loss streak > 5 consecutive days triggers circuit breaker
- Execution latency > 5min for signal generation triggers alert

## Circuit Breakers & Kill Switch Criteria  
- Single day drawdown >15% of portfolio: HALT IMMEDIATELY
- Cumulative weekly drawdown >25%: PAUSE operations 
- System downtime >4 hours: AUTO-stop new positions

## Phase-in Plan
1. Start with micro positions (0.1% of capital per trade)
2. Scale to small positions (0.5% of capital per trade) after 2 weeks of 65%+ daily accuracy
3. Progress to normal positions (1-2% of capital per trade) after 1 month of sustained success
4. Continue increasing position size gradually with performance

