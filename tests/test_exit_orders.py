"""決済注文 (利益確定・損切り) のテスト。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from trade_sim.portfolio import (
    LONG,
    SHORT,
    STOP_LOSS,
    TAKE_PROFIT,
    Portfolio,
    PortfolioError,
    Position,
    exit_prices,
    find_exit,
)
from trade_sim.simulator import InputError, Simulator

from .fakes import FakeSource

try:
    import pandas  # noqa: F401
except ImportError:  # pragma: no cover
    pandas = None

NOW = datetime(2026, 11, 4, 12, 0)
T = [datetime(2026, 11, 4, 10, 0 + 5 * i) for i in range(6)]  # 10:00, 10:05, ... (5 分足)


def candle(t, o, h, low):
    return (t, o, h, low)


class FindExitTest(unittest.TestCase):
    def long(self, tp=None, sl=None):
        return Position(1, "USDJPY", LONG, 10000, 150.0, "2026-11-04 09:58", 1.0, tp, sl, "2026-11-04 09:58")

    def short(self, tp=None, sl=None):
        return Position(1, "USDJPY", SHORT, 10000, 150.0, "2026-11-04 09:58", 1.0, tp, sl, "2026-11-04 09:58")

    def test_no_orders(self):
        self.assertIsNone(find_exit(self.long(), [candle(T[0], 150, 155, 145)], 160, NOW))

    def test_long_take_profit(self):
        candles = [candle(T[0], 150.0, 150.4, 149.8), candle(T[1], 150.3, 150.6, 150.2)]
        self.assertEqual(find_exit(self.long(tp=150.5, sl=149.5), candles), (T[1], 150.5, TAKE_PROFIT))

    def test_long_stop_loss(self):
        candles = [candle(T[0], 150.0, 150.2, 149.6), candle(T[1], 149.7, 149.8, 149.4)]
        self.assertEqual(find_exit(self.long(tp=150.5, sl=149.5), candles), (T[1], 149.5, STOP_LOSS))

    def test_short_take_profit_and_stop_loss(self):
        self.assertEqual(
            find_exit(self.short(tp=149.5, sl=150.5), [candle(T[0], 150.0, 150.1, 149.4)]), (T[0], 149.5, TAKE_PROFIT)
        )
        self.assertEqual(
            find_exit(self.short(tp=149.5, sl=150.5), [candle(T[0], 150.0, 150.6, 149.9)]), (T[0], 150.5, STOP_LOSS)
        )

    def test_gap_fills_at_open(self):
        # 損切りの価格を飛び越えて始まったら、始値 (より不利な価格) で約定する
        self.assertEqual(find_exit(self.long(sl=149.5), [candle(T[0], 148.0, 148.5, 147.0)]), (T[0], 148.0, STOP_LOSS))
        # 利益確定の価格を飛び越えて始まったら、始値 (より有利な価格) で約定する
        self.assertEqual(
            find_exit(self.short(tp=149.5), [candle(T[0], 148.0, 148.5, 147.0)]), (T[0], 148.0, TAKE_PROFIT)
        )

    def test_both_in_one_candle_counts_as_stop_loss(self):
        self.assertEqual(
            find_exit(self.long(tp=150.5, sl=149.5), [candle(T[0], 150.0, 151.0, 149.0)]), (T[0], 149.5, STOP_LOSS)
        )

    def test_latest_price_when_no_candles(self):
        self.assertEqual(find_exit(self.long(sl=149.5), [], 149.0, NOW), (NOW, 149.5, STOP_LOSS))
        self.assertIsNone(find_exit(self.long(sl=149.5), [], 149.9, NOW))

    def test_exit_prices_from_amounts(self):
        long = self.long()
        self.assertEqual(exit_prices(long, 5000, 3000), (150.5, 149.7))
        short = self.short()
        self.assertEqual(exit_prices(short, 5000, 3000), (149.5, 150.3))
        self.assertEqual(exit_prices(long, None, 0), (None, None))
        with self.assertRaises(PortfolioError):
            exit_prices(long, None, 2_000_000)  # 損切りの価格が 0 以下になる

    def test_open_with_amounts_sets_prices(self):
        pf = Portfolio()
        position = pf.open("KIOXIA", LONG, 300, 3000.0, "2026-11-02", take_profit_yen=30000, stop_loss_yen=15000)
        self.assertEqual((position.take_profit, position.stop_loss), (3100.0, 2950.0))
        self.assertEqual(position.exit_set_at, "2026-11-02")


class SimulatorExitOrderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "portfolio.json"
        self.source = FakeSource(latest={"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0})
        self.now = datetime(2026, 11, 4, 9, 58)
        self.sim = Simulator(self.path, self.source, clock=lambda: self.now)

    def positions(self):
        return Portfolio.load(self.path).positions

    def test_open_with_exit_orders(self):
        result = self.sim.open("usdjpy", "long", 100000, leverage=500, take_profit_yen=50000, stop_loss_yen=30000)
        self.assertIn("利確 150.500 (+50,000円)・損切り 149.700 (-30,000円)", result.lines[-1])
        position = self.positions()[0]
        self.assertEqual((position.leverage, position.exit_set_at), (500.0, "2026-11-04 09:58"))

    @unittest.skipIf(pandas is None, "pandas が必要")
    def test_exit_is_found_in_past_candles(self):
        self.sim.open("usdjpy", "long", 100000, leverage=25, take_profit_yen=50000, stop_loss_yen=30000)
        # 注文の前の足 (09:55) は判定に使わない。10:05 の足で損切りの価格 149.7 に届いた
        self.source.paths["USDJPY"] = [
            (datetime(2026, 11, 4, 9, 55), 150.0, 151.0, 148.0, 150.0),
            (datetime(2026, 11, 4, 10, 0), 150.0, 150.2, 149.8, 149.9),
            (datetime(2026, 11, 4, 10, 5), 149.9, 150.0, 149.6, 149.6),
            (datetime(2026, 11, 4, 10, 10), 149.6, 150.8, 149.5, 150.7),
        ]
        self.now = datetime(2026, 11, 4, 12, 0)  # アプリを閉じていた間に動いた
        result = self.sim.check_triggers()
        self.assertIn("決済値 149.700 (損切り 2026-11-04 10:05)  損益 -30,000円", result.lines[0])
        trade = Portfolio.load(self.path).trades[0]
        self.assertEqual((trade.reason, trade.closed_at, trade.exit_price), ("stop_loss", "2026-11-04 10:05", 149.7))
        self.assertEqual(self.positions(), [])

    @unittest.skipIf(pandas is None, "pandas が必要")
    def test_daily_candles_record_the_date(self):
        self.sim.open("kioxia", "short", 100, day=None, take_profit_yen=10000)
        self.source.paths["KIOXIA"] = [(datetime(2026, 11, 5), 2950.0, 2960.0, 2850.0, 2900.0)]
        self.source.path_interval = "1d"
        self.now = datetime(2026, 11, 6, 9, 0)
        self.sim.check_triggers()
        trade = Portfolio.load(self.path).trades[0]
        self.assertEqual((trade.closed_at, trade.exit_price, trade.pnl), ("2026-11-05", 2900.0, 10000.0))

    def test_latest_price_triggers_without_candles(self):
        self.sim.open("eurjpy", "short", 10000, stop_loss_yen=10000)  # 損切り 161.0
        self.source.latest_prices["EURJPY"] = 161.5
        result = self.sim.positions()
        self.assertIn("損切り", result.lines[0])
        self.assertEqual(self.positions(), [])

    def test_set_exit_keeps_and_cancels(self):
        self.sim.open("usdjpy", "long", 100000, leverage=25, take_profit_yen=50000)
        self.sim.set_exit(1, stop_loss_yen=30000)  # 利確はそのまま
        position = self.positions()[0]
        self.assertEqual((position.take_profit, position.stop_loss), (150.5, 149.7))
        self.sim.set_exit(1, take_profit_yen=0)  # 0 は取り消し
        position = self.positions()[0]
        self.assertEqual((position.take_profit, position.stop_loss), (None, 149.7))
        result = self.sim.set_exit("#1", take_profit_yen=0, stop_loss_yen=0)
        self.assertIn("取り消しました", result.lines[0])
        self.assertIsNone(self.positions()[0].exit_set_at)

    def test_set_exit_rejects_prices_already_reached(self):
        self.sim.open("usdjpy", "long", 100000, leverage=25)
        self.source.latest_prices["USDJPY"] = 149.0  # 今 -100,000 円
        with self.assertRaises(InputError) as cm:
            self.sim.set_exit(1, stop_loss_yen=50000)
        self.assertIn("すでに損切りの価格に届いています", str(cm.exception))

    def test_set_exit_resets_the_start_time(self):
        self.sim.open("usdjpy", "long", 100000, leverage=25, stop_loss_yen=30000)
        self.now = datetime(2026, 11, 4, 15, 0)
        self.sim.set_exit(1, stop_loss_yen=40000)
        self.assertEqual(self.positions()[0].exit_set_at, "2026-11-04 15:00")

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(InputError):
            self.sim.open("usdjpy", "long", 10000, take_profit_yen=-1)


if __name__ == "__main__":
    unittest.main()
