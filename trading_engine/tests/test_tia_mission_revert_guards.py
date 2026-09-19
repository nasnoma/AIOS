"""Regression: TIA mission disabled must not bypass exposure; ARB unlock pauses gone; fee-proof helper OK."""
from datetime import datetime, timezone

from trading_engine.spot.mission_tia_recovery import (
    SLEEVE_USD,
    allows_exposure_bypass,
    is_tia,
    mission_active,
    mission_buy_room_usd,
    recovery_sell_price,
    status,
)
from trading_engine.spot.unlock_calendar import is_unlock_buy_paused, iter_unlock_dates
from trading_engine.config import spot_settings


def test_mission_disabled():
    assert SLEEVE_USD == 0.0
    assert mission_active("TIA/USDT") is False
    assert allows_exposure_bypass("TIA/USDT") is False
    assert mission_buy_room_usd("TIA/USDT", equity=10_000, holding_usd=1_200) == 0.0
    st = status()
    assert st.get("active") is False or float(st.get("sleeve_usd") or 0) == 0.0


def test_arb_not_unlock_paused():
    assert not any(s.startswith("ARB") for s, _ in iter_unlock_dates())
    paused, _ = is_unlock_buy_paused("ARB/USDT", now=datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert paused is False


def test_tia_unlock_pad_still_works():
    paused, reason = is_unlock_buy_paused("TIA/USDT", now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert paused is True
    assert "TIA" in reason


def test_reserve_restored():
    assert abs(float(spot_settings.usdt_hard_reserve_pct) - 0.10) < 1e-9


def test_recovery_sell_price_fee_proof():
    px = recovery_sell_price(0.4474, 1000.0, fee_rate=0.001)
    # must clear fee-proof floor
    floor = 0.4474 * 1.001 / 0.999
    assert px >= floor


def test_is_tia():
    assert is_tia("TIA/USDT")
    assert not is_tia("ARB/USDT")


if __name__ == "__main__":
    test_mission_disabled()
    test_arb_not_unlock_paused()
    test_tia_unlock_pad_still_works()
    test_reserve_restored()
    test_recovery_sell_price_fee_proof()
    test_is_tia()
    print("OK")
