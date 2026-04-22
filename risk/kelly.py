"""
kelly.py — Kelly criterion position sizing utilities.
"""

def half_kelly_contracts(edge: float, cost_per_contract: float,
                         bankroll: float, max_usd: float) -> int:
    """
    Returns the half-Kelly recommended number of contracts.
    edge: net profit fraction (e.g. 0.02 for 2%)
    cost_per_contract: total cost per arb pair (e.g. 0.93)
    bankroll: available capital in USD
    max_usd: hard cap on position size in USD
    """
    if edge <= 0 or cost_per_contract <= 0:
        return 0
    f_star = edge / (1.0 - cost_per_contract)
    half_k = f_star * 0.5
    kelly_usd = bankroll * half_k
    capped_usd = min(kelly_usd, max_usd)
    return max(1, int(capped_usd / cost_per_contract))
