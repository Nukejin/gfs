"""YFinanceSource のテスト。yfinance の代わりに同じ形の DataFrame を返す偽モジュールを使う。"""

import sys
import types
import unittest
from datetime import date
from unittest import mock

from trade_sim.instruments import find_instrument
from trade_sim.prices import PriceError, YFinanceSource

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


def _frame(rows, tz="Asia/Tokyo"):
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows]).tz_localize(tz)
    closes = [c for _, c in rows]
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes, "Volume": 0},
        index=index,
    )


@unittest.skipIf(pd is None, "pandas が必要")
class YFinanceSourceTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.frame = pd.DataFrame()

        test = self

        class FakeTicker:
            def __init__(self, ticker):
                self.ticker = ticker

            def history(self, **kwargs):
                test.calls.append((self.ticker, kwargs))
                if isinstance(test.frame, Exception):
                    raise test.frame
                return test.frame

        patcher = mock.patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=FakeTicker)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_latest_uses_last_close_and_previous_close(self):
        self.frame = _frame([("2026-10-29", 3000.0), ("2026-10-30", 3100.0), ("2026-11-02", float("nan"))])
        quote = YFinanceSource().latest(find_instrument("kioxia"))
        self.assertEqual((quote.price, quote.as_of, quote.prev_close), (3100.0, date(2026, 10, 30), 3000.0))
        ticker, kwargs = self.calls[0]
        self.assertEqual(ticker, "285A.T")
        self.assertEqual(kwargs["interval"], "1d")
        self.assertFalse(kwargs["auto_adjust"])

    def test_latest_is_cached(self):
        self.frame = _frame([("2026-10-30", 150.12345)], tz="Europe/London")
        source = YFinanceSource()
        inst = find_instrument("usdjpy")
        self.assertEqual(source.latest(inst).price, 150.123)
        source.latest(inst)
        self.assertEqual(len(self.calls), 1)

    def test_latest_is_fetched_again_after_cache_expires_or_is_cleared(self):
        self.frame = _frame([("2026-10-30", 150.0)], tz="Europe/London")
        inst = find_instrument("usdjpy")
        source = YFinanceSource(cache_seconds=0)
        source.latest(inst)
        source.latest(inst)
        self.assertEqual(len(self.calls), 2)

        source = YFinanceSource()
        source.latest(inst)
        source.clear_cache()
        source.latest(inst)
        self.assertEqual(len(self.calls), 4)

    def test_on_date_falls_back_to_previous_trading_day(self):
        self.frame = _frame([("2026-10-29", 19000.0), ("2026-10-30", 19500.0)])
        quote = YFinanceSource().on_date(find_instrument("lasertec"), date(2026, 11, 1))  # 日曜日
        self.assertEqual((quote.price, quote.as_of), (19500.0, date(2026, 10, 30)))
        _, kwargs = self.calls[0]
        self.assertEqual((kwargs["start"], kwargs["end"]), ("2026-10-18", "2026-11-02"))

    def test_on_date_ignores_rows_after_the_date(self):
        self.frame = _frame([("2026-10-29", 160.0), ("2026-10-30", 161.0)], tz="Europe/London")
        quote = YFinanceSource().on_date(find_instrument("eurjpy"), date(2026, 10, 29))
        self.assertEqual((quote.price, quote.as_of), (160.0, date(2026, 10, 29)))

    def test_empty_data_raises(self):
        self.frame = pd.DataFrame()
        source = YFinanceSource()
        with self.assertRaises(PriceError):
            source.latest(find_instrument("kioxia"))
        with self.assertRaises(PriceError):
            source.on_date(find_instrument("kioxia"), date(2024, 1, 5))

    def test_network_error_becomes_price_error(self):
        self.frame = RuntimeError("Too Many Requests")
        with self.assertRaises(PriceError) as cm:
            YFinanceSource().latest(find_instrument("usdjpy"))
        self.assertIn("Too Many Requests", str(cm.exception))


class MissingYFinanceTest(unittest.TestCase):
    def test_missing_yfinance_gives_install_hint(self):
        with mock.patch.dict(sys.modules, {"yfinance": None}):
            with self.assertRaises(PriceError) as cm:
                YFinanceSource().latest(find_instrument("kioxia"))
        self.assertIn("pip install", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
