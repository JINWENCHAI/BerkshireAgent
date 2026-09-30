"""BerkshireAgent market-quote helper.

Free public quote sources, no API key. Used by the ``market-quote`` skill and
indirectly by the persona skills when they need a hard price number.

Stable URL surface:
- Yahoo Finance ``/v8/finance/chart/<ticker>?interval=1d&range=5d`` — returns
  intraday history for one ticker, plus the chart's ``meta`` block (currency,
  exchange, regular-market price). We take the last ``close`` and the previous
  trading day's close for ``change``.
- Sina ``hq.sinajs.cn`` — A-share / HK free feed. Format:
  ``var hq_str_<tag>="<name>,<open>,<prev_close>,<last>,...``, parsed with a
  hand-rolled regex to avoid dragging in lxml.

The helper is intentionally synchronous. The persona skills call it through
``python_repl`` (a sync tool); wrapping it in async would not save anything.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

UA = "BerkshireAgent/0.1 (market-quote; +https://github.com/JINWENCHAI/BerkshireAgent)"

# US / global indices → Yahoo ticker mapping. A-share codes need a market
# prefix; we infer that from the ticker shape in ``_yahoo_ticker``.
_INDEX_ALIASES = {
    "NASDAQ": "^IXIC",
    "NASDAQ-100": "^NDX",
    "S&P 500": "^GSPC",
    "SPX": "^GSPC",
    "DOW": "^DJI",
    "DJIA": "^DJI",
    "HANG SENG": "^HSI",
    "HSI": "^HSI",
    "CSI 300": "000300.SS",
    "SHANGHAI": "000001.SS",
    "SHENZHEN": "399001.SZ",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "OIL": "CL=F",
    "BITCOIN": "BTC-USD",
    "ETH": "ETH-USD",
}


def _yahoo_ticker(ticker: str) -> str:
    raw = ticker.strip()
    if not raw:
        return raw
    upper = raw.upper()
    if upper in _INDEX_ALIASES:
        return _INDEX_ALIASES[upper]
    # A-share 6-digit codes: 6xxxxx → Shanghai (.SS), 0/3xx xxxx → Shenzhen (.SZ).
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("60", "68", "90")):
            return f"{raw}.SS"
        if raw.startswith(("00", "30", "20")):
            return f"{raw}.SZ"
    # HK 4-/5-digit codes: pad to 4 digits and add .HK. Must NOT prefix
    # 6-digit A-share codes, which the rule above already handled.
    if re.fullmatch(r"\d{1,5}", raw) and not raw.startswith(("60", "68", "90", "00", "30", "20")):
        return f"{raw.zfill(4)}.HK"
    return raw


def _fetch_yahoo(ticker: str, *, timeout: float = 10.0) -> dict[str, Any] | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": "1d", "range": "5d"}
    headers = {"User-Agent": UA}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        logger.exception("yahoo request failed for %s", ticker)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning("yahoo non-200 for %s: %s", ticker, resp.status_code)
        return None
    body = resp.json()
    chart = body.get("chart", {})
    result = (chart.get("result") or [{}])[0]
    meta = result.get("meta") or {}
    closes = (result.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    closes = [c for c in closes if c is not None]
    if not closes or meta.get("regularMarketPrice") is None:
        return None
    last = float(meta["regularMarketPrice"])
    prev_close = float(closes[-2]) if len(closes) >= 2 else float(meta.get("chartPreviousClose") or last)
    change = last - prev_close
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0
    ts = meta.get("regularMarketTime")
    asof = (
        datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        if ts
        else datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    )
    return {
        "ticker": ticker,
        "name": meta.get("longName") or meta.get("shortName") or ticker,
        "currency": meta.get("currency") or "USD",
        "last": round(last, 4),
        "change": round(change, 4),
        "change_pct": round(change_pct, 2),
        "as_of": asof,
        "source": "Yahoo Finance",
        "url": f"https://finance.yahoo.com/quote/{ticker}",
    }


def _fetch_sina(ticker: str, *, timeout: float = 8.0) -> dict[str, Any] | None:
    """Sina quote feed for A-share / HK. Free, ~1-min delayed."""
    # Sina expects the *raw* code, not the Yahoo-suffixed one.
    raw = ticker.split(".")[0]
    url = f"https://hq.sinajs.cn/list={raw}"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": UA}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        logger.exception("sina request failed for %s", ticker)
        return None
    if resp.status_code != 200:
        return None
    text = resp.content.decode("gbk", errors="ignore")
    # Format: var hq_str_<tag>="<name>,<open>,<prev_close>,<last>,<high>,<low>,...";
    m = re.search(r'"([^"]*)"', text)
    if not m:
        return None
    fields = m.group(1).split(",")
    if len(fields) < 6 or not fields[0]:
        return None
    name, open_p, prev_close, last, high, low = fields[:6]
    try:
        last_f = float(last)
        prev_f = float(prev_close)
    except ValueError:
        return None
    change = last_f - prev_f
    change_pct = (change / prev_f * 100.0) if prev_f else 0.0
    return {
        "ticker": ticker,
        "name": name,
        "currency": "CNY" if ticker.endswith((".SS", ".SZ")) else "HKD",
        "last": round(last_f, 4),
        "change": round(change, 4),
        "change_pct": round(change_pct, 2),
        "as_of": datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source": "Sina Finance",
        "url": f"https://finance.sina.com.cn/realstock/company/{ticker}/",
    }


def _quote_one(ticker: str) -> dict[str, Any] | None:
    yt = _yahoo_ticker(ticker)
    # Try Yahoo first (covers US, global indices, futures, crypto).
    out = _fetch_yahoo(yt)
    if out is not None:
        return out
    # Fall back to Sina for A-share / HK raw codes.
    if yt.endswith((".SS", ".SZ", ".HK")):
        return _fetch_sina(yt)
    return None


def quote(ticker: str) -> dict[str, Any] | None:
    """Public entry point. See SKILL.md for the return shape."""
    return _quote_one(ticker)


def quote_many(tickers: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tickers:
        q = _quote_one(t)
        if q is not None:
            out.append(q)
    return out


def _cli(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    if not argv:
        print("usage: python tools/market_quote.py <ticker> [<ticker> ...]")
        return 1
    for t in argv:
        q = quote(t)
        if q is None:
            print(f"{t}: not found")
        else:
            print(
                f"{q['ticker']:>10}  {q['name'][:32]:32}  {q['last']:>12,.4f}  {q['currency']}  "
                f"{q['change']:>+8,.2f} ({q['change_pct']:>+5.2f}%)  @ {q['as_of']}  [{q['source']}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))