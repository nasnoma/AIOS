"""
trading_engine/market_hours.py

Market hours guard for all asset classes.
Prevents scanning and order execution during closed market sessions.

Asset classes:
  CRYPTO         — 24/7, never restricted
  STOCK_CFD      — US NYSE/NASDAQ hours (Mon–Fri), extended hours supported
  PRECIOUS_METAL — Gold/Silver forex session (Mon–Fri, Sat closed, Sun opens 22:00 UTC)

All internal time comparisons use UTC.
Log messages show WAT (+01:00) for Nigerian users.
"""
from __future__ import annotations

import datetime
from enum import Enum
from typing import Optional
from loguru import logger


# ── Asset class enum ──────────────────────────────────────────────────────────

class AssetClass(str, Enum):
    CRYPTO         = "crypto"
    STOCK          = "stock"
    STOCK_CFD      = "stock_cfd"
    PRECIOUS_METAL = "precious_metal"


# ── Symbol classification ─────────────────────────────────────────────────────

_PRECIOUS_METAL_PREFIXES = ("XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER", "CL", "USOIL")

# Bybit crypto linear perpetual tickers (so we don't mis-classify them as stock CFDs)
_KNOWN_CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "MATIC", "LINK",
    "UNI", "ATOM", "LTC", "DOGE", "SHIB", "OP", "ARB", "APT", "SUI", "TRX",
    "TON", "NEAR", "FIL", "INJ", "SEI", "WLD", "PEPE", "FLOKI", "BONK", "JUP",
    "PYTH", "STRK", "MANTA", "ZK", "W", "ENA", "ETHFI", "RUNE", "SAND", "MANA",
    "1INCH", "AAVE", "CRV", "MKR", "SNX", "SUSHI", "YFI", "COMP", "BAL",
    "AGIX", "FET", "OCEAN", "RNDR", "GRT", "LDO", "BLUR", "GMX", "DYDX",
    "PENDLE", "ORDI", "SATS", "1000SATS", "BOME", "NOT", "IO", "ZRO",
    "EIGEN", "OMNI", "REZ", "SAGA", "PORTAL", "DYM", "PIXELS", "MAVIA",
    "TNSR", "MERL", "ETHW", "LUNA", "LUNC", "UST",
}


def classify_symbol(symbol: str) -> AssetClass:
    """
    Classify a trading symbol into its asset class.

    Examples:
        'BTC/USDT'          → CRYPTO
        'BTC/USDT:USDT'     → CRYPTO
        'XAU/USDT:USDT'     → PRECIOUS_METAL
        'XAGUSD'            → PRECIOUS_METAL
        'AAPL/USDT:USDT'    → STOCK_CFD
        'TSLA/USDT:USDT'    → STOCK_CFD
        'ETH/USDT:USDT'     → CRYPTO  (ETH is a known crypto ticker)
        'AAPL'              → STOCK
    """
    s = symbol.upper()

    # Gold / Silver / Platinum / Palladium first (highest priority)
    for prefix in _PRECIOUS_METAL_PREFIXES:
        if s.startswith(prefix):
            return AssetClass.PRECIOUS_METAL

    # Bybit linear perpetuals end with :USDT or :USDC
    if s.endswith(":USDT") or s.endswith(":USDC"):
        ticker = s.split("/")[0]
        if ticker in _KNOWN_CRYPTO_TICKERS:
            return AssetClass.CRYPTO
        # Unknown ticker with linear perpetual suffix → treat as stock CFD
        return AssetClass.STOCK_CFD

    # Everything else: check if it's a known crypto or contains '/'
    if "/" in s or s in _KNOWN_CRYPTO_TICKERS:
        return AssetClass.CRYPTO

    # If it is a plain alphabetical string (e.g. AAPL, TSLA), it is a plain STOCK
    return AssetClass.STOCK


# ── NYSE holiday calculation ───────────────────────────────────────────────────

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """Return the nth occurrence of weekday (0=Mon … 6=Sun) in the given month."""
    count = 0
    d = datetime.date(year, month, 1)
    while d.month == month:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += datetime.timedelta(days=1)
    raise ValueError(f"No {n}th weekday={weekday} in {year}-{month:02d}")


def _last_weekday_of_month(year: int, month: int, weekday: int) -> datetime.date:
    """Return the last occurrence of weekday in the given month."""
    if month == 12:
        d = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    while d.weekday() != weekday:
        d -= datetime.timedelta(days=1)
    return d


def _easter_sunday(year: int) -> datetime.date:
    """Compute Easter Sunday via the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lv = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lv) // 451
    month = (h + lv - 7 * m + 114) // 31
    day   = (h + lv - 7 * m + 114) % 31 + 1
    return datetime.date(year, month, day)


def _nyse_holidays(year: int) -> set[datetime.date]:
    """Return the set of NYSE market holiday dates for the given year (observed)."""
    holidays: set[datetime.date] = set()

    def add(d: datetime.date) -> None:
        """Observe: Saturday → Friday, Sunday → Monday."""
        if d.weekday() == 5:
            d -= datetime.timedelta(days=1)
        elif d.weekday() == 6:
            d += datetime.timedelta(days=1)
        holidays.add(d)

    add(datetime.date(year, 1, 1))    # New Year's Day
    add(datetime.date(year, 6, 19))   # Juneteenth National Independence Day
    add(datetime.date(year, 7, 4))    # Independence Day
    add(datetime.date(year, 12, 25))  # Christmas Day

    add(_nth_weekday_of_month(year, 1,  0, 3))  # MLK Day: 3rd Monday Jan
    add(_nth_weekday_of_month(year, 2,  0, 3))  # Presidents' Day: 3rd Monday Feb
    add(_last_weekday_of_month(year,  5, 0))     # Memorial Day: last Monday May
    add(_nth_weekday_of_month(year, 9,  0, 1))  # Labor Day: 1st Monday Sep
    add(_nth_weekday_of_month(year, 11, 3, 4))  # Thanksgiving: 4th Thursday Nov

    # Good Friday — NYSE closes
    add(_easter_sunday(year) - datetime.timedelta(days=2))

    return holidays


def _next_trading_day(from_date: datetime.date, year_holidays: set[datetime.date]) -> datetime.date:
    """Return the next NYSE trading day after from_date."""
    d = from_date + datetime.timedelta(days=1)
    while d.weekday() >= 5 or d in year_holidays:
        if d.year != from_date.year:
            year_holidays = _nyse_holidays(d.year)
        d += datetime.timedelta(days=1)
    return d


# ── MarketStatus result object ────────────────────────────────────────────────

class MarketStatus:
    """Result of a market hours check."""

    def __init__(
        self,
        is_open: bool,
        reason: str,
        next_open: Optional[datetime.datetime] = None,
    ):
        self.is_open   = is_open
        self.reason    = reason
        self.next_open = next_open  # UTC-aware datetime

    def __bool__(self) -> bool:
        return self.is_open

    def __repr__(self) -> str:
        return f"MarketStatus(open={self.is_open}, reason={self.reason!r})"

    def log(self, symbol: str) -> None:
        """Emit a log line. Closed = INFO (notable), Open = DEBUG (routine)."""
        icon = "✅" if self.is_open else "⏸️"
        msg  = f"Market {icon} | {symbol} | {self.reason}"
        if not self.is_open and self.next_open:
            # Display in WAT (UTC+1) for Nigerian users
            wat = self.next_open + datetime.timedelta(hours=1)
            msg += f" | Opens: {wat.strftime('%a %d %b %Y %H:%M WAT')}"
        if self.is_open:
            logger.debug(msg)
        else:
            logger.info(msg)


# ── Per-asset-class checks ────────────────────────────────────────────────────

def _check_stock_cfd(now_utc: datetime.datetime, extended: bool = True) -> MarketStatus:
    """
    US Stock CFD market hours check.

    Extended hours (extended=True):
        Summer (EDT, UTC-4): 13:00 – 21:00 UTC  [9 AM pre-market → 5 PM after-hours]
        Winter (EST, UTC-5): 14:00 – 22:00 UTC

    Core hours (extended=False):
        Summer (EDT):        13:30 – 20:00 UTC  [9:30 AM – 4:00 PM NYSE]
        Winter (EST):        14:30 – 21:00 UTC
    """
    year = now_utc.year

    # DST: EDT starts 2nd Sunday in March, ends 1st Sunday in November
    dst_start = _nth_weekday_of_month(year, 3,  6, 2)  # 2nd Sunday March
    dst_end   = _nth_weekday_of_month(year, 11, 6, 1)  # 1st Sunday November
    in_edt    = dst_start <= now_utc.date() < dst_end

    if in_edt:
        open_utc  = datetime.time(13, 0) if extended else datetime.time(13, 30)
        close_utc = datetime.time(21, 0) if extended else datetime.time(20, 0)
        tz_label  = "EDT"
    else:
        open_utc  = datetime.time(14, 0) if extended else datetime.time(14, 30)
        close_utc = datetime.time(22, 0) if extended else datetime.time(21, 0)
        tz_label  = "EST"

    now_date  = now_utc.date()
    weekday   = now_utc.weekday()   # 0=Mon … 6=Sun
    holidays  = _nyse_holidays(year)

    # Weekend
    if weekday >= 5:
        days_to_mon = 7 - weekday
        next_mon    = now_date + datetime.timedelta(days=days_to_mon)
        next_open   = datetime.datetime.combine(next_mon, open_utc,
                                                tzinfo=datetime.timezone.utc)
        return MarketStatus(False, "Weekend — NYSE closed Sat & Sun", next_open)

    # Holiday
    if now_date in holidays:
        next_td   = _next_trading_day(now_date, holidays)
        next_open = datetime.datetime.combine(next_td, open_utc,
                                              tzinfo=datetime.timezone.utc)
        return MarketStatus(False, f"NYSE holiday ({now_date})", next_open)

    now_time = now_utc.time()

    # Before open
    if now_time < open_utc:
        next_open = datetime.datetime.combine(now_date, open_utc,
                                              tzinfo=datetime.timezone.utc)
        label = "pre-market" if extended else "core session"
        return MarketStatus(
            False,
            f"Before US stock {label} opens ({open_utc.strftime('%H:%M')} UTC / {tz_label})",
            next_open,
        )

    # After close
    if now_time >= close_utc:
        next_td   = _next_trading_day(now_date, holidays)
        next_open = datetime.datetime.combine(next_td, open_utc,
                                              tzinfo=datetime.timezone.utc)
        label = "extended hours" if extended else "core session"
        return MarketStatus(
            False,
            f"US stock {label} closed ({close_utc.strftime('%H:%M')} UTC / {tz_label})",
            next_open,
        )

    session = "Extended hours" if extended else "Core hours"
    return MarketStatus(True, f"{session} open ({tz_label} / {open_utc.strftime('%H:%M')}–{close_utc.strftime('%H:%M')} UTC)")


def _check_precious_metal(now_utc: datetime.datetime) -> MarketStatus:
    """
    Gold / Silver / Platinum / Palladium market hours.

    Trading window: Sunday 22:00 UTC → Friday 21:00 UTC.
    All day Saturday: fully closed.
    """
    weekday  = now_utc.weekday()
    now_time = now_utc.time()

    # Saturday — closed all day
    if weekday == 5:
        sun = now_utc.date() + datetime.timedelta(days=1)
        next_open = datetime.datetime.combine(sun, datetime.time(22, 0),
                                              tzinfo=datetime.timezone.utc)
        return MarketStatus(False, "Precious metals weekend closure (all day Saturday)", next_open)

    # Sunday — only open after 22:00 UTC
    if weekday == 6:
        if now_time < datetime.time(22, 0):
            next_open = datetime.datetime.combine(now_utc.date(), datetime.time(22, 0),
                                                  tzinfo=datetime.timezone.utc)
            return MarketStatus(False,
                                "Precious metals Sunday not yet open (opens 22:00 UTC)",
                                next_open)
        return MarketStatus(True, "Precious metals Sunday session open (22:00+ UTC)")

    # Friday — closes at 21:00 UTC
    if weekday == 4 and now_time >= datetime.time(21, 0):
        sat     = now_utc.date() + datetime.timedelta(days=1)
        sun     = sat + datetime.timedelta(days=1)
        next_open = datetime.datetime.combine(sun, datetime.time(22, 0),
                                              tzinfo=datetime.timezone.utc)
        return MarketStatus(False, "Precious metals Friday session closed (after 21:00 UTC)", next_open)

    # Monday–Thursday: open 24 h
    day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][weekday]
    return MarketStatus(True, f"Precious metals {day_name} session open (24 h)")


# ── Public API ────────────────────────────────────────────────────────────────

def market_status(symbol: str, extended_stock_hours: bool = True) -> MarketStatus:
    """
    Return full MarketStatus for a symbol.

    Args:
        symbol:               Any trading symbol (BTC/USDT, AAPL/USDT:USDT, XAU/USDT:USDT …)
        extended_stock_hours: If True (default), include pre-market & after-hours for stock CFDs.
    """
    asset_class = classify_symbol(symbol)
    now_utc     = datetime.datetime.now(tz=datetime.timezone.utc)

    if asset_class == AssetClass.CRYPTO:
        return MarketStatus(True, "Crypto — 24/7, always open")

    if asset_class == AssetClass.PRECIOUS_METAL:
        return _check_precious_metal(now_utc)

    if asset_class in (AssetClass.STOCK_CFD, AssetClass.STOCK):
        return _check_stock_cfd(now_utc, extended=extended_stock_hours)

    return MarketStatus(True, f"Unknown asset class for {symbol} — defaulting open")


def is_market_open(symbol: str, extended_stock_hours: bool = True, *, silent: bool = False) -> bool:
    """
    Quick boolean market-open check.
    Logs the status unless silent=True.
    """
    status = market_status(symbol, extended_stock_hours=extended_stock_hours)
    if not silent:
        status.log(symbol)
    return status.is_open


def next_open_utc(symbol: str) -> Optional[datetime.datetime]:
    """Return the next market open (UTC) or None if already open."""
    status = market_status(symbol)
    return None if status.is_open else status.next_open


def filter_open_symbols(symbols: list[str], extended_stock_hours: bool = True) -> list[str]:
    """
    Filter a list of symbols to only those whose market is currently open.
    Logs one summary line per closed symbol.
    """
    open_syms:   list[str] = []
    closed_syms: list[str] = []

    for sym in symbols:
        status = market_status(sym, extended_stock_hours=extended_stock_hours)
        if status.is_open:
            open_syms.append(sym)
        else:
            closed_syms.append(sym)
            status.log(sym)

    if closed_syms:
        logger.info(f"⏸️  {len(closed_syms)} symbol(s) skipped (market closed): {closed_syms}")
    return open_syms
