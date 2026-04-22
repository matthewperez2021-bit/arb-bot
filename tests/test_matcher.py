"""
test_matcher.py — Unit tests for market matching pipeline.
"""
from clients.normalizer import NormalizedMarket
from matching.matcher import MarketMatcher


def test_normalize_strips_stopwords_and_punctuation():
    m = MarketMatcher()
    # "win" is intentionally retained (not a stopword) as a semantic signal in many market titles.
    assert m.normalize("Will the Democrat win the 2024 presidential election?") == "democrat win 2024 presidential election"


def test_false_match_primary_vs_general_is_rejected():
    m = MarketMatcher()
    conflict, pair = m.has_conflicting_qualifiers(
        "Will Trump win the Republican primary?",
        "Will Trump win the general election?",
    )
    assert conflict is True
    assert pair in ("primary/general", "general/primary")


def test_find_matches_returns_top_candidates():
    matcher = MarketMatcher()
    kalshi = [
        NormalizedMarket(
            platform="kalshi",
            market_id="K1",
            title="Will Democrats win the 2024 US presidential election?",
            close_time="2030-11-05T23:59:00Z",
            yes_token="K1",
            no_token="K1",
        )
    ]
    poly = [
        NormalizedMarket(
            platform="polymarket",
            market_id="P_good",
            title="Will the Democrat win the 2024 presidential election?",
            close_time="2030-11-05T23:59:00Z",
            yes_token="Y",
            no_token="N",
        ),
        NormalizedMarket(
            platform="polymarket",
            market_id="P_bad",
            title="Will the Republican win the 2024 presidential election?",
            close_time="2030-11-05T23:59:00Z",
            yes_token="Y2",
            no_token="N2",
        ),
    ]

    matches = matcher.find_matches(kalshi, poly, threshold=0.0, max_per_kalshi=2)
    assert len(matches) >= 1
    # Best should be the semantically similar one
    assert matches[0].poly.market_id == "P_good"
