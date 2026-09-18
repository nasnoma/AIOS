"""Regression: RegimeParams must be per-engine copies, not shared globals."""
from dataclasses import replace

# Lightweight stand-in if full GridEngine import needs ccxt at module import —
# still exercise the real module when deps exist.
def test_replace_keeps_template_immutable():
    from trading_engine.spot.grid_engine import REGIME_PARAMS
    tmpl = REGIME_PARAMS["RANGE"]
    copy = replace(tmpl)
    copy.buy_levels = 0
    assert tmpl.buy_levels > 0
    assert copy.buy_levels == 0


def test_engines_isolate_buy_levels_zero():
    from trading_engine.spot.grid_engine import REGIME_PARAMS, GridEngine
    bull_before = REGIME_PARAMS["BULL"].buy_levels
    assert bull_before > 0

    a = GridEngine("AAA/USDT", allocated_usd=500.0)
    b = GridEngine("BBB/USDT", allocated_usd=500.0)
    a.set_regime("BULL")
    b.set_regime("BULL")

    assert a.params is not b.params
    assert a.params is not REGIME_PARAMS["BULL"]
    assert b.params.buy_levels == bull_before

    a.params.buy_levels = 0
    assert a.params.buy_levels == 0
    assert b.params.buy_levels == bull_before
    assert REGIME_PARAMS["BULL"].buy_levels == bull_before

    a.restore_regime_params()
    assert a.params.buy_levels == bull_before


if __name__ == "__main__":
    test_replace_keeps_template_immutable()
    try:
        test_engines_isolate_buy_levels_zero()
    except ModuleNotFoundError as e:
        print("skip full engine test (missing dep):", e)
    else:
        print("engine isolation OK")
    print("OK")
