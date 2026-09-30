"""Unit tests for ``tools.market_quote``.

These exercise the ticker-shape inference and the failure paths; they do NOT
hit Yahoo/Sina. The live lookup smoke is in the SKILL.md ``if __name__ == '__main__'``
entry point — run it manually.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_TOOLS_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tools", "market_quote.py"))
_spec = importlib.util.spec_from_file_location("berkshire_market_quote", _TOOLS_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"could not load market_quote from {_TOOLS_PATH}")
mq = importlib.util.module_from_spec(_spec)
sys.modules["berkshire_market_quote"] = mq
_spec.loader.exec_module(mq)


@pytest.mark.parametrize("input_ticker,expected", [
    # Index aliases
    ("NASDAQ", "^IXIC"),
    ("SPX", "^GSPC"),
    ("DJIA", "^DJI"),
    ("HSI", "^HSI"),
    ("GOLD", "GC=F"),
    ("BITCOIN", "BTC-USD"),
    # A-share / HK auto-prefixing
    ("600519", "600519.SS"),  # 茅台
    ("000001", "000001.SZ"),  # 平安银行
    ("000858", "000858.SZ"),  # 五粮液
    ("688981", "688981.SS"),  # 中芯国际
    ("0700", "0700.HK"),      # 腾讯 (no leading zero)
    # Already-shaped tickers pass through
    ("AAPL", "AAPL"),
    ("^GSPC", "^GSPC"),
    ("600519.SS", "600519.SS"),
    ("BRK.B", "BRK.B"),
    # Lowercase aliases still resolve
    ("nasdaq", "^IXIC"),
    ("gold", "GC=F"),
])
def test_yahoo_ticker(input_ticker, expected):
    assert mq._yahoo_ticker(input_ticker) == expected


def test_yahoo_ticker_strips_whitespace():
    assert mq._yahoo_ticker("  AAPL  ") == "AAPL"


def test_yahoo_ticker_empty_passthrough():
    # Empty input should not blow up; downstream call will return None.
    assert mq._yahoo_ticker("") == ""


def test_quote_returns_none_for_404(monkeypatch):
    class FakeResp:
        status_code = 404
        def json(self): return {}

    def fake_get(*args, **kwargs):
        return FakeResp()

    monkeypatch.setattr(mq.httpx, "get", fake_get)
    assert mq._fetch_yahoo("BOGUS") is None


def test_quote_many_skips_unresolved(monkeypatch):
    calls = {"AAPL": {"ticker": "AAPL", "name": "Apple"}, "BOGUS": None}
    monkeypatch.setattr(mq, "_quote_one", lambda t: calls.get(t))
    out = mq.quote_many(["AAPL", "BOGUS"])
    assert len(out) == 1
    assert out[0]["ticker"] == "AAPL"