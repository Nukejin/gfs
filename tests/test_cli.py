import contextlib
import io
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from trade_sim.cli import main
from trade_sim.portfolio import Portfolio

from .fakes import FakeSource

NOW = datetime(2026, 11, 3, 10, 0)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "portfolio.json"
        self.source = FakeSource(
            latest={"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0},
            history={
                "LASERTEC": {date(2026, 10, 29): 19000.0, date(2026, 10, 30): 19500.0},
                "EURJPY": {date(2026, 10, 30): 161.0},
            },
        )

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--file", str(self.path), *args], source=self.source, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def test_long_and_short_round_trip_with_total_pnl(self):
        self.assertEqual(self.run_cli("long", "kioxia", "100")[0], 0)
        code, out, _ = self.run_cli("short", "ドル円", "10000")
        self.assertEqual(code, 0)
        self.assertIn("新規ショート: #2 ドル円 10,000通貨 @ 150.000円", out)

        # 価格が動く: キオクシア +200円、ドル円 -0.5円
        self.source.latest_prices.update(KIOXIA=3200.0, USDJPY=149.5)

        _, out, _ = self.run_cli("positions")
        self.assertIn("+20,000円", out)
        self.assertIn("+5,000円", out)
        self.assertIn("評価損益の合計: +25,000円", out)

        code, out, _ = self.run_cli("close", "1")
        self.assertEqual(code, 0)
        self.assertIn("損益 +20,000円", out)
        self.assertIn("実現損益の通算: +20,000円", out)

        _, out, _ = self.run_cli("summary")
        self.assertIn("通算損益: +25,000円  (実現損益 +20,000円 + 評価損益 +5,000円)", out)

        _, out, _ = self.run_cli("history")
        self.assertIn("実現損益の合計: +20,000円 (1 件)", out)

    def test_short_loses_when_price_rises(self):
        self.run_cli("short", "lasertec", "100")
        self.source.latest_prices["LASERTEC"] = 21000.0
        _, out, _ = self.run_cli("close", "#1")
        self.assertIn("損益 -100,000円", out)

    def test_historical_trade_on_holiday_uses_previous_close(self):
        code, out, _ = self.run_cli("long", "6920", "100", "--date", "2026-11-01")
        self.assertEqual(code, 0, out)
        self.assertIn("@ 19,500.0円 (2026-11-01 は取引がないため、直前の営業日 2026-10-30 の終値)", out)
        position = Portfolio.load(self.path).positions[0]
        self.assertEqual((position.entry_price, position.opened_at), (19500.0, "2026-10-30"))

    def test_historical_trade_on_trading_day(self):
        _, out, _ = self.run_cli("long", "lasertec", "100", "--date", "2026-10-29")
        self.assertIn("@ 19,000.0円 (2026-10-29 の終値)", out)

    def test_date_of_today_is_labeled_as_latest(self):
        self.source.history["USDJPY"] = {NOW.date(): 151.5}
        _, out, _ = self.run_cli("short", "usdjpy", "10000", "--date", "2026-11-03")
        self.assertIn("@ 151.500円 (当日 2026-11-03 の最新値)", out)

    def test_close_before_open_date_is_rejected(self):
        self.run_cli("long", "lasertec", "100", "--date", "2026-10-30")
        code, _, err = self.run_cli("close", "1", "--date", "2026-10-29")
        self.assertEqual(code, 1)
        self.assertIn("より前です", err)
        self.assertEqual(len(Portfolio.load(self.path).positions), 1)

    def test_manual_price_and_partial_close(self):
        self.run_cli("long", "eurjpy", "10000", "--price", "160.5")
        _, out, _ = self.run_cli("close", "1", "--qty", "4000", "--price", "161")
        self.assertIn("損益 +2,000円", out)
        pf = Portfolio.load(self.path)
        self.assertEqual(pf.positions[0].qty, 6000)
        self.assertEqual(pf.trades[0].closed_at, "2026-11-03 10:00")

    def test_close_all(self):
        self.run_cli("long", "kioxia", "100")
        self.run_cli("short", "usdjpy", "1000")
        self.source.latest_prices.update(KIOXIA=2900.0, USDJPY=151.0)
        code, out, _ = self.run_cli("close", "all")
        self.assertEqual(code, 0)
        self.assertIn("実現損益の通算: -11,000円", out)
        self.assertEqual(Portfolio.load(self.path).positions, [])
        self.assertIn("決済する建玉がありません", self.run_cli("close", "all")[1])

    def test_failed_close_saves_nothing(self):
        self.run_cli("long", "kioxia", "100")
        self.run_cli("long", "usdjpy", "1000")
        del self.source.latest_prices["USDJPY"]  # 2 件目の価格が取れない
        code, _, err = self.run_cli("close", "1", "2")
        self.assertEqual(code, 1)
        self.assertIn("ドル円", err)
        self.assertEqual(len(Portfolio.load(self.path).positions), 2)

    def test_summary_without_price_excludes_unrealized(self):
        self.run_cli("long", "kioxia", "100")
        del self.source.latest_prices["KIOXIA"]
        code, out, _ = self.run_cli("pnl")
        self.assertEqual(code, 0)
        self.assertIn("通算損益: 0円", out)
        self.assertIn("含まれていません", out)

    def test_summary_lists_every_instrument(self):
        _, out, _ = self.run_cli("summary")
        for name in ("キオクシア", "レーザーテック", "ドル円", "ユーロ円", "合計"):
            self.assertIn(name, out)
        self.assertIn("通算損益: 0円", out)

    def test_errors(self):
        cases = [
            (("long", "toyota", "100"), "対応していません"),
            (("long", "kioxia", "100", "--date", "2026-12-01"), "未来の日付"),
            (("close", "5"), "見つかりません"),
            (("close", "abc"), "数字で指定"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                code, _, err = self.run_cli(*args)
                self.assertEqual(code, 1)
                self.assertIn(message, err)

    def test_qty_with_multiple_ids_is_rejected(self):
        self.run_cli("long", "kioxia", "100")
        self.run_cli("long", "kioxia", "100")
        code, _, err = self.run_cli("close", "1", "2", "--qty", "50")
        self.assertEqual(code, 1)
        self.assertIn("一部決済", err)

    def test_quote(self):
        del self.source.latest_prices["EURJPY"]
        code, out, _ = self.run_cli("quote")
        self.assertEqual(code, 0)
        self.assertIn("20,000.0", out)
        self.assertIn("※ ユーロ円", out)

    def test_leverage_and_account(self):
        code, out, _ = self.run_cli("short", "usdjpy", "100000", "--leverage", "25")
        self.assertEqual(code, 0)
        self.assertIn("レバレッジ 25倍・必要証拠金 600,000円", out)

        _, out, _ = self.run_cli("account")
        self.assertIn("必要証拠金: 600,000円", out)
        self.assertIn("余力: 9,400,000円", out)

        code, out, _ = self.run_cli("account", "--capital", "2000000", "--losscut", "30")
        self.assertEqual(code, 0)
        self.assertIn("初期資金を 2,000,000円 にしました。", out)
        self.assertIn("ロスカット水準: 30%", out)

        _, out, _ = self.run_cli("positions")
        self.assertIn("25倍", out)
        self.assertIn("600,000円", out)

    def test_leverage_errors(self):
        code, _, err = self.run_cli("long", "kioxia", "100", "-l", "5")
        self.assertEqual(code, 1)
        self.assertIn("3.3倍まで", err)
        code, _, err = self.run_cli("long", "lasertec", "10000")
        self.assertEqual(code, 1)
        self.assertIn("余力が足りません", err)

    def test_reset(self):
        self.run_cli("long", "kioxia", "100")
        code, out, _ = self.run_cli("reset", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
