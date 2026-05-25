# -*- coding: utf-8 -*-
"""Regression tests for Akshare historical fallback timeout handling."""

import multiprocessing
import sys
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from data_provider.akshare_fetcher import AkshareFetcher, _akshare_call_with_timeout


def _sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def test_akshare_call_with_timeout_returns_promptly() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="unit-hang"):
        _akshare_call_with_timeout(
            _sleep_for,
            0.2,
            timeout=0.01,
            call_name="unit-hang",
        )

    assert time.monotonic() - started < 0.5


def test_akshare_call_with_timeout_reaps_timed_out_worker_process() -> None:
    call_name = "unit-hang-reap"

    with pytest.raises(TimeoutError, match=call_name):
        _akshare_call_with_timeout(
            _sleep_for,
            5,
            timeout=0.01,
            call_name=call_name,
        )

    leaked = [
        process
        for process in multiprocessing.active_children()
        if process.name == f"akshare-{call_name}"
    ]
    assert leaked == []


@pytest.mark.parametrize(
    ("method_name", "api_name", "call_name"),
    [
        ("_fetch_stock_data_sina", "stock_zh_a_daily", "ak.stock_zh_a_daily"),
        ("_fetch_stock_data_tx", "stock_zh_a_hist_tx", "ak.stock_zh_a_hist_tx"),
    ],
)
def test_sina_and_tencent_history_calls_use_timeout_wrapper(
    monkeypatch,
    method_name: str,
    api_name: str,
    call_name: str,
) -> None:
    captured = {}

    def fake_call(func, *args, timeout=None, call_name="", **kwargs):
        captured["func"] = func
        captured["timeout"] = timeout
        captured["call_name"] = call_name
        captured["kwargs"] = kwargs
        return pd.DataFrame(
            {
                "date": ["2026-05-25"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "amount": [20000],
            }
        )

    fake_api_func = object()
    fake_akshare = SimpleNamespace(**{api_name: fake_api_func})
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setattr("data_provider.akshare_fetcher._akshare_call_with_timeout", fake_call)

    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    fetcher._history_call_timeout = 7

    method = getattr(fetcher, method_name)
    df = method("605218", "2026-05-01", "2026-05-25")

    assert captured["func"] is fake_api_func
    assert captured["timeout"] == 7
    assert captured["call_name"] == call_name
    assert captured["kwargs"]["symbol"] == "sh605218"
    assert captured["kwargs"]["start_date"] == "20260501"
    assert captured["kwargs"]["end_date"] == "20260525"
    assert captured["kwargs"]["adjust"] == "qfq"
    assert list(df.columns)[:7] == ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]


def test_stock_data_falls_back_after_sina_timeout(monkeypatch) -> None:
    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    tx_df = pd.DataFrame({"日期": ["2026-05-25"], "收盘": [10.2]})

    monkeypatch.setattr(fetcher, "_fetch_stock_data_em", lambda *args: pd.DataFrame())
    monkeypatch.setattr(
        fetcher,
        "_fetch_stock_data_sina",
        lambda *args: (_ for _ in ()).throw(TimeoutError("sina timeout")),
    )
    monkeypatch.setattr(fetcher, "_fetch_stock_data_tx", lambda *args: tx_df)

    result = fetcher._fetch_stock_data("605218", "2026-05-01", "2026-05-25")

    assert result is tx_df
