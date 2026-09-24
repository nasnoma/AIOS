"""Soft alerts: rate-limit, reserve near-floor, slow_bleed; never pause buys."""
from __future__ import annotations

import time

import pytest

from trading_engine.spot import soft_alerts as sa
from trading_engine.spot.unlock_calendar import (
    HARD_PAUSE_ALLOWLIST,
    AdvisoryUnlock,
    audit_unlock_coverage,
    is_unlock_buy_paused,
    unlock_status_summary,
)
from datetime import date, datetime, timezone


@pytest.fixture(autouse=True)
def _reset():
    sa.reset_soft_alerts_for_tests()
    # shrink cooldown for tests
    sa.ALERT_COOLDOWN_SEC = 60.0
    yield
    sa.reset_soft_alerts_for_tests()
    sa.ALERT_COOLDOWN_SEC = 1800.0


def test_reserve_near_floor_triggers_within_2pct():
    near, why = sa.check_reserve_near_floor(1010.0, 1000.0)
    assert near is True
    assert "near reserve floor" in why


def test_reserve_far_above_no_alert():
    near, why = sa.check_reserve_near_floor(1500.0, 1000.0)
    assert near is False
    assert why == ""


def test_slow_bleed_btc_threshold():
    on, why = sa.check_slow_bleed(
        btc_72h_pct=-8.1, equity_dd_72h=0.01, cascade_latch_active=False
    )
    assert on is True
    assert "BTC 72h" in why
    assert "no buy pause" in why


def test_slow_bleed_equity_dd_threshold():
    on, why = sa.check_slow_bleed(
        btc_72h_pct=-1.0, equity_dd_72h=0.11, cascade_latch_active=False
    )
    assert on is True
    assert "equity DD" in why


def test_slow_bleed_suppressed_when_cascade_latch_active():
    on, why = sa.check_slow_bleed(
        btc_72h_pct=-12.0, equity_dd_72h=0.20, cascade_latch_active=True
    )
    assert on is False
    assert "cascade latch" in why


def test_rate_limit_does_not_spam(monkeypatch):
    fired = []

    def _capture(key, msg, now, level="info"):
        fired.append((key, msg, now))
        sa._state.last_fired_ts[key] = now
        sa._state.fire_counts[key] = sa._state.fire_counts.get(key, 0) + 1
        sa._state.last_reasons[key] = msg

    monkeypatch.setattr(sa, "_fire", _capture)
    t0 = 1_000_000.0
    sa.update_soft_alerts(
        usdt_free=1000.0,
        usdt_reserved=1000.0,
        equity=10_000.0,
        btc_72h_pct=-9.0,
        cascade_latch_active=False,
        now=t0,
    )
    sa.update_soft_alerts(
        usdt_free=1000.0,
        usdt_reserved=1000.0,
        equity=10_000.0,
        btc_72h_pct=-9.0,
        cascade_latch_active=False,
        now=t0 + 10.0,  # within cooldown
    )
    keys = [k for k, _, _ in fired]
    assert keys.count("reserve_near_floor") == 1
    assert keys.count("slow_bleed_watch") == 1

    # after cooldown, can fire again
    sa.update_soft_alerts(
        usdt_free=1000.0,
        usdt_reserved=1000.0,
        equity=10_000.0,
        btc_72h_pct=-9.0,
        cascade_latch_active=False,
        now=t0 + 70.0,
    )
    assert [k for k, _, _ in fired].count("slow_bleed_watch") == 2


def test_soft_alerts_never_claim_pause():
    """Contract: summary thresholds note says log-only / never pauses."""
    summary = sa.update_soft_alerts(
        usdt_free=900.0,
        usdt_reserved=1000.0,
        equity=9_000.0,
        btc_72h_pct=-10.0,
        cascade_latch_active=False,
        now=time.time(),
    )
    note = summary["thresholds"]["note"]
    assert "never pauses" in note.lower() or "log-only" in note.lower()
    # module has no pause API
    assert not hasattr(sa, "is_buy_paused")
    assert not hasattr(sa, "should_pause_buys")


def test_equity_72h_peak_drawdown():
    t0 = 2_000_000.0
    sa.update_soft_alerts(equity=10_000.0, now=t0, cascade_latch_active=True)
    sa.update_soft_alerts(equity=10_500.0, now=t0 + 60, cascade_latch_active=True)
    # drop 12% from peak
    out = sa.update_soft_alerts(
        equity=10_500.0 * 0.88,
        btc_72h_pct=0.0,
        cascade_latch_active=False,
        now=t0 + 120,
    )
    assert out["equity_dd_72h_pct"] >= 10.0
    assert out["slow_bleed_watch"] is True


def test_non_allowlist_symbol_never_hard_paused():
    paused, reason = is_unlock_buy_paused(
        "OP/USDT", now=datetime(2026, 9, 23, tzinfo=timezone.utc)
    )
    assert paused is False
    assert reason == ""
    assert "OP/USDT" not in HARD_PAUSE_ALLOWLIST


def test_audit_reports_gaps_and_next_30d():
    report = audit_unlock_coverage(
        ["ARB/USDT", "TIA/USDT", "OP/USDT", "SEI/USDT", "APT/USDT"],
        within_days=30,
        now=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    assert "ARB/USDT" in report["covered_explicit"]
    assert "TIA/USDT" in report["covered_explicit"]
    assert "OP/USDT" in report["gaps"] or "OP/USDT" in report["soft_only"]
    assert "SEI/USDT" in report["gaps"] or "SEI/USDT" in report["soft_only"]
    assert "OP/USDT" not in report["covered_explicit"]
    symbols_next = {r["symbol"] for r in report["next_30d"] if r.get("kind") == "explicit"}
    assert "ARB/USDT" in symbols_next or "TIA/USDT" in symbols_next
    st = unlock_status_summary(["ARB/USDT", "TIA/USDT", "OP/USDT"], now=datetime(2026, 9, 24, tzinfo=timezone.utc))
    assert any(p["symbol"] == "ARB/USDT" for p in st["active_hard_pauses"])
    assert any(p["symbol"] == "TIA/USDT" for p in st["active_hard_pauses"])


def test_advisory_hard_pause_flag_ignored_without_allowlist(monkeypatch):
    """Even if someone sets hard_pause=True on advisory row, is_unlock_buy_paused stays False off-allowlist."""
    import trading_engine.spot.unlock_calendar as uc

    fake = [
        AdvisoryUnlock(
            symbol="OP/USDT",
            unlock_date=date(2026, 9, 24),
            source="test",
            note="should not hard pause",
            hard_pause=True,
            enabled=True,
        )
    ]
    monkeypatch.setattr(uc, "_ADVISORY_UNLOCKS", fake)
    try:
        paused, _ = uc.is_unlock_buy_paused("OP/USDT", now=datetime(2026, 9, 24, tzinfo=timezone.utc))
        assert paused is False
    finally:
        # belt-and-suspenders if caller monkeypatch does not restore
        if hasattr(monkeypatch, "undo"):
            monkeypatch.undo()


def test_equity_wipe_to_zero_reports_full_drawdown():
    """Equity 10000 → 0 must show ~100% DD and arm slow_bleed (not clear it)."""
    t0 = 3_000_000.0
    sa.update_soft_alerts(equity=10_000.0, now=t0, cascade_latch_active=True)
    out = sa.update_soft_alerts(
        equity=0.0,
        btc_72h_pct=0.0,
        cascade_latch_active=False,
        now=t0 + 60,
    )
    assert out["equity_peak_72h"] >= 10_000.0
    assert out["equity_dd_72h_pct"] >= 99.0
    assert out["slow_bleed_watch"] is True

