"""Simulator のレバレッジ・余力・ロスカットのテスト。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from trade_sim.portfolio import LOSSCUT, Portfolio
from trade_sim.simulator import InputError, Simulator

from .fakes import FakeSource

NOW = datetime(2026, 11, 3, 10, 0)


class SimulatorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "portfolio.json"
        self.source = FakeSource(latest={"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0})
        self.sim = Simulator(self.path, self.source, clock=lambda: NOW)

    def portfolio(self):
        return Portfolio.load(self.path)

    def test_open_with_leverage(self):
        result = self.sim.open("usdjpy", "short", 100000, leverage=25)
        self.assertIn("レバレッジ 25倍・必要証拠金 600,000円", result.lines[0])
        self.assertEqual(self.portfolio().positions[0].leverage, 25.0)

    def test_leverage_above_limit_is_rejected(self):
        with self.assertRaises(InputError) as cm:
            self.sim.open("kioxia", "long", 100, leverage=4)
        self.assertIn("3.3倍まで", str(cm.exception))
        self.assertEqual(self.portfolio().positions, [])

    def test_order_larger_than_free_margin_is_rejected(self):
        # 初期資金 1,000 万円: レーザーテック 2,000株 = 4,000 万円は 3.3 倍でも 1,212 万円の証拠金が要る
        with self.assertRaises(InputError) as cm:
            self.sim.open("lasertec", "long", 2000, leverage=3.3)
        self.assertIn("余力が足りません", str(cm.exception))
        self.sim.open("lasertec", "long", 1500, leverage=3.3)  # 909 万円なら建てられる
        with self.assertRaises(InputError):
            self.sim.open("kioxia", "long", 1000, leverage=3)  # 残りの余力 (約 91 万円) では足りない

    def test_free_margin_includes_unrealized_pnl(self):
        self.sim.open("usdjpy", "long", 100000, leverage=25)  # 必要証拠金 60 万円
        self.source.latest_prices["USDJPY"] = 155.0  # 評価損益 +50 万円
        self.sim.refresh_prices()
        status, _ = self.sim.account_status()
        self.assertEqual(status.free_margin, 10_000_000 + 500_000 - 600_000)

    def test_unknown_price_blocks_new_orders(self):
        self.sim.open("kioxia", "long", 100)
        del self.source.latest_prices["KIOXIA"]
        with self.assertRaises(InputError) as cm:
            self.sim.open("usdjpy", "long", 10000)
        self.assertIn("余力を計算できません", str(cm.exception))

    def test_losscut_closes_all_positions(self):
        self.sim.set_account(initial_capital=1_000_000)
        self.sim.open("usdjpy", "long", 100000, leverage=25)  # 必要証拠金 60 万円
        self.sim.open("kioxia", "short", 100)  # 必要証拠金 30 万円
        self.assertIsNone(self.sim.check_losscut())

        # 有効証拠金 100 万 - 60 万 = 40 万円、必要証拠金 90 万円 → 維持率 44% < 50%
        self.source.latest_prices["USDJPY"] = 144.0
        result = self.sim.check_losscut()
        self.assertIn("ロスカット水準 50% を下回った", result.lines[0])
        portfolio = self.portfolio()
        self.assertEqual(portfolio.positions, [])
        self.assertEqual([t.reason for t in portfolio.trades], [LOSSCUT, LOSSCUT])
        self.assertEqual(portfolio.realized_pnl, -600_000)
        self.assertIsNone(self.sim.check_losscut())

    def test_losscut_happens_before_an_order(self):
        self.sim.set_account(initial_capital=1_000_000)
        self.sim.open("usdjpy", "long", 100000, leverage=25)
        self.source.latest_prices["USDJPY"] = 142.0  # 維持率 33%
        result = self.sim.open("kioxia", "long", 100)
        self.assertFalse(result.ok)
        self.assertIn("注文は出していません", result.notes[0])
        self.assertEqual(self.portfolio().positions, [])

    def test_losscut_is_reported_by_positions_and_summary(self):
        self.sim.set_account(initial_capital=1_000_000)
        self.sim.open("usdjpy", "long", 100000, leverage=25)
        self.source.latest_prices["USDJPY"] = 142.0  # 維持率 33%
        self.assertTrue(self.sim.positions().lines[0].startswith("ロスカット:"))
        self.assertIn("通算損益: -800,000円", "\n".join(self.sim.summary().lines))

    def test_losscut_can_be_turned_off(self):
        self.sim.set_account(initial_capital=1_000_000, losscut_level=0)
        self.sim.open("usdjpy", "long", 100000, leverage=25)
        self.source.latest_prices["USDJPY"] = 140.0  # 有効証拠金 0 円
        self.assertIsNone(self.sim.check_losscut())
        self.assertEqual(len(self.portfolio().positions), 1)

    def test_account(self):
        self.sim.open("kioxia", "long", 300, leverage=3)
        lines = self.sim.account().lines
        self.assertIn("有効証拠金: 10,000,000円", lines[0])
        self.assertIn("必要証拠金: 300,000円", lines[1])
        self.assertIn("余力: 9,700,000円", lines[1])
        self.assertIn("証拠金維持率: 3,333.3%", lines[1])

    def test_set_account(self):
        result = self.sim.set_account(initial_capital=5_000_000, losscut_level=0)
        self.assertEqual(result.lines, ["初期資金を 5,000,000円 にしました。", "ロスカット水準を なし (ロスカットしない) にしました。"])
        portfolio = self.portfolio()
        self.assertEqual((portfolio.initial_capital, portfolio.losscut_level), (5_000_000, 0))

    def test_history_shows_leverage_and_losscut(self):
        self.sim.set_account(initial_capital=1_000_000)
        self.sim.open("usdjpy", "long", 100000, leverage=25)
        self.source.latest_prices["USDJPY"] = 142.0  # 維持率 33%
        self.sim.check_losscut()
        row = self.sim.history().table.rows[0]
        self.assertIn("25倍", row)
        self.assertEqual(row[-1], "ロスカット")


if __name__ == "__main__":
    unittest.main()
