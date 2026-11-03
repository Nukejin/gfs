"""チャート (charts.py) と、ローソク足の取得 (YFinanceSource.candles) のテスト。"""

import sys
import types
import unittest
from datetime import datetime
from unittest import mock

from trade_sim.instruments import find_instrument
from trade_sim.portfolio import Position, Trade
from trade_sim.prices import PriceError, YFinanceSource, get_timeframe

from .fakes import FakeSource

try:
    import pandas as pd
    import plotly  # noqa: F401

    from trade_sim import charts
except ImportError:  # pragma: no cover
    charts = None


@unittest.skipIf(charts is None, "pandas と plotly が必要")
class LocateTest(unittest.TestCase):
    def setUp(self):
        self.index = pd.DatetimeIndex(["2026-10-30 09:00", "2026-10-30 09:05", "2026-11-02 09:00"])

    def test_locate(self):
        self.assertIsNone(charts.locate(self.index, datetime(2026, 10, 30, 8, 59)))
        self.assertEqual(charts.locate(self.index, datetime(2026, 10, 30, 9, 0)), 0)
        self.assertEqual(charts.locate(self.index, datetime(2026, 10, 30, 9, 3)), 0)
        self.assertEqual(charts.locate(self.index, datetime(2026, 10, 31, 12, 0)), 1)  # 休日は直前の足
        self.assertEqual(charts.locate(self.index, datetime(2026, 11, 3, 12, 0)), 2)

    def test_trade_time(self):
        self.assertEqual(charts.trade_time("2026-11-03 10:15"), datetime(2026, 11, 3, 10, 15))
        # 終値で約定した取引 (日付だけ) は、その日の取引終了後として扱う
        self.assertEqual(charts.trade_time("2026-10-30"), datetime(2026, 10, 30, 23, 59))

    def test_visible_start(self):
        daily = pd.date_range("2025-11-03", "2026-11-02", freq="D")
        start = charts.visible_start(daily, "6mo")
        self.assertEqual(daily[start], pd.Timestamp("2026-05-02"))
        self.assertEqual(charts.visible_start(daily, "max"), 0)

        intraday = pd.DatetimeIndex(
            [f"2026-10-{d} {h}:00" for d in (27, 28, 29, 30, 31) for h in (9, 10)] + ["2026-11-02 09:00"]
        )
        self.assertEqual(intraday[charts.visible_start(intraday, "1d")], pd.Timestamp("2026-11-02 09:00"))
        self.assertEqual(intraday[charts.visible_start(intraday, "5d")], pd.Timestamp("2026-10-28 09:00"))


class FetchPeriodTest(unittest.TestCase):
    def test_fetch_one_step_longer_within_yahoo_limits(self):
        self.assertEqual(get_timeframe("1d").fetch_period("6mo"), "1y")
        self.assertEqual(get_timeframe("1wk").fetch_period("max"), "max")
        self.assertEqual(get_timeframe("5m").fetch_period("5d"), "1mo")
        self.assertEqual(get_timeframe("5m").fetch_period("1mo"), "1mo")  # 5分足は 60 日までしか取れない
        self.assertEqual(get_timeframe("1h").fetch_period("1y"), "2y")


@unittest.skipIf(charts is None, "pandas と plotly が必要")
class CandleFigureTest(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource(latest={"KIOXIA": 3000.0, "USDJPY": 150.0})
        self.daily = get_timeframe("1d")

    def _names(self, fig):
        return [trace.name for trace in fig.data]

    def test_stock_chart_has_volume_moving_averages_and_trade_marks(self):
        inst = find_instrument("kioxia")
        candles = self.source.candles(inst, self.daily, "6mo")  # 2026-09-24 〜 2026-11-02 の日足 40 本
        trades = [Trade(1, "KIOXIA", "long", 100, 2990.0, 3010.0, "2026-10-01", "2026-10-15 10:00")]
        positions = [Position(2, "KIOXIA", "short", 100, 3020.0, "2026-10-20 13:00")]
        fig = charts.candle_figure(inst, candles, self.daily, "6mo", trades, positions)

        names = self._names(fig)
        self.assertIn("キオクシア", names)
        self.assertIn("出来高", names)
        self.assertTrue({"5日線", "25日線"} <= set(names))
        long_marks = fig.data[names.index("ロングで建てた位置")]
        self.assertEqual((list(long_marks.x), list(long_marks.y)), (["2026/10/01"], [2990.0]))
        exits = fig.data[names.index("決済した位置")]
        self.assertEqual(list(exits.x), ["2026/10/15"])
        self.assertIn("+2,000円", exits.text[0])
        self.assertIn("ショートで建てた位置", names)
        self.assertEqual([shape.y0 for shape in fig.layout.shapes], [3020.0])  # 保有中の建値の線

    def test_fx_chart_has_no_volume(self):
        inst = find_instrument("usdjpy")
        fig = charts.candle_figure(inst, self.source.candles(inst, self.daily, "6mo"), self.daily, "6mo")
        self.assertNotIn("出来高", self._names(fig))

    def test_only_the_requested_period_is_shown(self):
        inst = find_instrument("kioxia")
        candles = self.source.candles(inst, self.daily, "6mo")
        fig = charts.candle_figure(inst, candles, self.daily, "1mo")
        shown = list(fig.data[0].x)
        self.assertEqual(shown[0], "2026/10/02")
        self.assertEqual(shown[-1], "2026/11/02")
        # 表示範囲の前の足も移動平均線の計算に使うので、5日線は左端から値がある
        ma5 = fig.data[self._names(fig).index("5日線")]
        self.assertFalse(pd.isna(ma5.y[0]))

    def test_trades_before_the_chart_are_not_marked(self):
        inst = find_instrument("kioxia")
        trades = [Trade(1, "KIOXIA", "long", 100, 2990.0, 3010.0, "2026-01-05", "2026-01-06")]
        fig = charts.candle_figure(inst, self.source.candles(inst, self.daily, "6mo"), self.daily, "6mo", trades)
        self.assertNotIn("ロングで建てた位置", self._names(fig))
        self.assertNotIn("決済した位置", self._names(fig))


@unittest.skipIf(charts is None, "pandas と plotly が必要")
class PnlFigureTest(unittest.TestCase):
    def test_realized_curve_is_in_time_order(self):
        trades = [
            Trade(2, "USDJPY", "short", 10000, 150.0, 149.0, "2026-10-01", "2026-10-20 10:00"),  # +10,000
            Trade(1, "KIOXIA", "long", 100, 3000.0, 2900.0, "2026-09-01", "2026-09-10"),  # -10,000
        ]
        curve = charts.realized_curve(trades)
        self.assertEqual(list(curve["pnl"]), [-10000.0, 10000.0])
        self.assertEqual(list(curve["cumulative"]), [-10000.0, 0.0])

    def test_equity_figure(self):
        trades = [Trade(1, "KIOXIA", "long", 100, 3000.0, 3100.0, "2026-09-01", "2026-09-10")]
        fig = charts.equity_figure(trades, 25000.0, datetime(2026, 11, 3, 10, 0))
        self.assertEqual(list(fig.data[0].y), [0.0, 10000.0])
        self.assertEqual(list(fig.data[1].y), [25000.0])

    def test_pnl_by_instrument_figure(self):
        from trade_sim.portfolio import SummaryRow

        rows = [SummaryRow("KIOXIA", 1, 1, 10000.0, 0, 0.0), SummaryRow("USDJPY", 0, 0, 0.0, 1, None)]
        fig = charts.pnl_by_instrument_figure(rows)
        self.assertEqual(list(fig.data[0].x), ["キオクシア", "ドル円"])
        self.assertEqual(list(fig.data[0].y), [10000.0, 0.0])


@unittest.skipIf(charts is None, "pandas が必要")
class CandlesFromYFinanceTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.frame = None
        test = self

        class FakeTicker:
            def __init__(self, ticker):
                self.ticker = ticker

            def history(self, **kwargs):
                test.calls.append(kwargs)
                return test.frame

        patcher = mock.patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=FakeTicker)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _frame(self, stamps, tz):
        index = pd.DatetimeIndex(stamps).tz_localize(tz)
        n = len(stamps)
        return pd.DataFrame(
            {"Open": [1.0] * n, "High": [2.0] * n, "Low": [0.5] * n, "Close": [1.5] * n, "Adj Close": [1.5] * n,
             "Volume": [0] * n},
            index=index,
        )

    def test_intraday_times_are_converted_to_japan_time(self):
        self.frame = self._frame(["2026-11-03 00:00", "2026-11-03 00:05"], "Europe/London")
        candles = YFinanceSource().candles(find_instrument("usdjpy"), get_timeframe("5m"), "5d")
        self.assertEqual(list(candles.index), [pd.Timestamp("2026-11-03 09:00"), pd.Timestamp("2026-11-03 09:05")])
        self.assertEqual((self.calls[0]["interval"], self.calls[0]["period"]), ("5m", "1mo"))

    def test_daily_candles_keep_their_date(self):
        self.frame = self._frame(["2026-10-30 00:00", "2026-11-02 00:00"], "Europe/London")
        candles = YFinanceSource().candles(find_instrument("eurjpy"), get_timeframe("1d"), "6mo")
        self.assertEqual(list(candles.index), [pd.Timestamp("2026-10-30"), pd.Timestamp("2026-11-02")])
        self.assertEqual(self.calls[0]["period"], "1y")

    def test_candles_are_cached(self):
        self.frame = self._frame(["2026-10-30 00:00"], "Asia/Tokyo")
        source = YFinanceSource()
        source.candles(find_instrument("kioxia"), get_timeframe("1d"), "6mo")
        source.candles(find_instrument("kioxia"), get_timeframe("1d"), "6mo")
        self.assertEqual(len(self.calls), 1)

    def test_no_data_raises(self):
        self.frame = pd.DataFrame()
        with self.assertRaises(PriceError):
            YFinanceSource().candles(find_instrument("kioxia"), get_timeframe("5m"), "1d")


if __name__ == "__main__":
    unittest.main()
