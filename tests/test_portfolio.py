import json
import tempfile
import unittest
from pathlib import Path

from trade_sim.portfolio import LONG, SHORT, Portfolio, PortfolioError, calc_pnl


class CalcPnlTest(unittest.TestCase):
    def test_long_profits_when_price_rises(self):
        self.assertEqual(calc_pnl(LONG, 3000, 3200, 100), 20000)

    def test_long_loses_when_price_falls(self):
        self.assertEqual(calc_pnl(LONG, 3000, 2900, 100), -10000)

    def test_short_profits_when_price_falls(self):
        self.assertEqual(calc_pnl(SHORT, 20000, 19000, 100), 100000)

    def test_short_loses_when_price_rises(self):
        self.assertEqual(calc_pnl(SHORT, 150.0, 151.25, 10000), -12500)

    def test_fx_float_noise_is_rounded(self):
        self.assertEqual(calc_pnl(LONG, 150.123, 152.345, 10000), 22220.0)


class PortfolioTest(unittest.TestCase):
    def test_open_assigns_sequential_ids(self):
        pf = Portfolio()
        a = pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02")
        b = pf.open("USDJPY", SHORT, 10000, 150.0, "2026-11-02")
        self.assertEqual((a.id, b.id), (1, 2))
        self.assertEqual(len(pf.positions), 2)

    def test_full_close_moves_position_to_trades(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02")
        trade = pf.close(1, 3200, "2026-11-03")
        self.assertEqual(trade.pnl, 20000)
        self.assertEqual(pf.positions, [])
        self.assertEqual(pf.realized_pnl, 20000)

    def test_partial_close(self):
        pf = Portfolio()
        pf.open("EURJPY", LONG, 10000, 160.0, "2026-11-02")
        first = pf.close(1, 161.0, "2026-11-02", qty=4000)
        self.assertEqual(first.pnl, 4000)
        self.assertEqual(pf.find(1).qty, 6000)
        second = pf.close(1, 159.0, "2026-11-03")
        self.assertEqual(second.qty, 6000)
        self.assertEqual(second.pnl, -6000)
        self.assertEqual(pf.positions, [])
        self.assertEqual(pf.realized_pnl, -2000)

    def test_close_more_than_held_fails(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02")
        with self.assertRaises(PortfolioError):
            pf.close(1, 3100, "2026-11-02", qty=200)
        self.assertEqual(pf.find(1).qty, 100)

    def test_close_before_open_date_fails(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02 10:00")
        with self.assertRaises(PortfolioError):
            pf.close(1, 3100, "2026-10-31")

    def test_unknown_position_fails(self):
        with self.assertRaises(PortfolioError):
            Portfolio().close(99, 100, "2026-11-02")

    def test_invalid_quantity_fails(self):
        pf = Portfolio()
        for qty in (0, -100, 1.5, True):
            with self.subTest(qty=qty), self.assertRaises(PortfolioError):
                pf.open("KIOXIA", LONG, qty, 3000, "2026-11-02")

    def test_invalid_side_fails(self):
        with self.assertRaises(PortfolioError):
            Portfolio().open("KIOXIA", "buy", 100, 3000, "2026-11-02")

    def test_summary(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02")
        pf.close(1, 3200, "2026-11-02")  # +20,000
        pf.open("KIOXIA", SHORT, 100, 3200, "2026-11-02")
        pf.close(2, 3300, "2026-11-02")  # -10,000
        pf.open("USDJPY", SHORT, 10000, 150.0, "2026-11-02")  # 保有中
        pf.open("LASERTEC", LONG, 100, 20000, "2026-11-02")  # 保有中 (現在値なし)

        rows = {r.symbol: r for r in pf.summary({"USDJPY": 149.5, "LASERTEC": None})}

        kioxia = rows["KIOXIA"]
        self.assertEqual((kioxia.trades, kioxia.wins, kioxia.realized), (2, 1, 10000))
        self.assertEqual(kioxia.win_rate, 0.5)
        self.assertEqual(kioxia.unrealized, 0)
        self.assertEqual(kioxia.total, 10000)

        usdjpy = rows["USDJPY"]
        self.assertEqual((usdjpy.open_positions, usdjpy.unrealized, usdjpy.total), (1, 5000, 5000))
        self.assertIsNone(usdjpy.win_rate)

        lasertec = rows["LASERTEC"]
        self.assertIsNone(lasertec.unrealized)
        self.assertIsNone(lasertec.total)

        self.assertEqual(rows["EURJPY"].total, 0)


class LeverageAndAccountTest(unittest.TestCase):
    def test_required_margin(self):
        pf = Portfolio()
        stock = pf.open("KIOXIA", LONG, 300, 3000.0, "2026-11-02", leverage=3)
        fx = pf.open("USDJPY", SHORT, 100000, 150.0, "2026-11-02", leverage=25)
        self.assertEqual(stock.required_margin, 300000)  # 900,000 / 3
        self.assertEqual(fx.required_margin, 600000)  # 15,000,000 / 25

    def test_leverage_limits(self):
        pf = Portfolio()
        pf.open("LASERTEC", LONG, 100, 20000.0, "2026-11-02", leverage=3.3)
        pf.open("EURJPY", LONG, 10000, 160.0, "2026-11-02", leverage=500)  # 為替は 500 倍まで
        for symbol, leverage in (("KIOXIA", 5), ("USDJPY", 501), ("USDJPY", 0.5), ("KIOXIA", True)):
            with self.subTest(symbol=symbol, leverage=leverage), self.assertRaises(PortfolioError):
                pf.open(symbol, LONG, 100, 1000.0, "2026-11-02", leverage=leverage)

    def test_status(self):
        pf = Portfolio(initial_capital=1_000_000)
        pf.open("KIOXIA", LONG, 100, 3000.0, "2026-11-01", leverage=1)
        pf.close(1, 3500.0, "2026-11-02")  # 実現損益 +50,000
        pf.open("USDJPY", LONG, 100000, 150.0, "2026-11-02", leverage=25)  # 必要証拠金 600,000

        status = pf.status({"USDJPY": 149.0})  # 評価損益 -100,000
        self.assertEqual(status.cash, 1_050_000)
        self.assertEqual(status.equity, 950_000)
        self.assertEqual(status.used_margin, 600_000)
        self.assertEqual(status.free_margin, 350_000)
        self.assertAlmostEqual(status.margin_level, 158.333, places=3)
        self.assertFalse(status.losscut_triggered)

        status = pf.status({"USDJPY": 145.0})  # 評価損益 -500,000 → 維持率 91.7%
        pf.set_account(losscut_level=100)
        self.assertTrue(pf.status({"USDJPY": 145.0}).losscut_triggered)
        self.assertFalse(status.losscut_triggered)

    def test_status_without_price(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000.0, "2026-11-02")
        status = pf.status({})
        self.assertIsNone(status.equity)
        self.assertIsNone(status.free_margin)
        self.assertIsNone(status.margin_level)
        self.assertFalse(status.losscut_triggered)

    def test_no_positions(self):
        status = Portfolio().status({})
        self.assertEqual((status.equity, status.free_margin, status.margin_level), (10_000_000, 10_000_000, None))

    def test_losscut_disabled(self):
        pf = Portfolio(losscut_level=0)
        pf.open("USDJPY", LONG, 100000, 150.0, "2026-11-02", leverage=25)
        self.assertFalse(pf.status({"USDJPY": 50.0}).losscut_triggered)

    def test_invalid_account_settings(self):
        pf = Portfolio()
        for kwargs in ({"initial_capital": 0}, {"losscut_level": -1}, {"losscut_level": 150}):
            with self.subTest(**kwargs), self.assertRaises(PortfolioError):
                pf.set_account(**kwargs)

    def test_close_records_leverage_and_reason(self):
        pf = Portfolio()
        pf.open("KIOXIA", SHORT, 100, 3000.0, "2026-11-02", leverage=2)
        trade = pf.close(1, 2900.0, "2026-11-03", reason="losscut")
        self.assertEqual((trade.leverage, trade.reason, trade.pnl), (2.0, "losscut", 10000))


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "portfolio.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty_portfolio(self):
        pf = Portfolio.load(self.path)
        self.assertEqual((pf.positions, pf.trades, pf.next_id), ([], [], 1))

    def test_round_trip(self):
        pf = Portfolio()
        pf.open("KIOXIA", LONG, 100, 3000, "2026-11-02")
        pf.open("USDJPY", SHORT, 10000, 150.123, "2026-11-02 10:00")
        pf.close(1, 3200, "2026-11-03")
        pf.save(self.path)

        loaded = Portfolio.load(self.path)
        self.assertEqual(loaded.positions, pf.positions)
        self.assertEqual(loaded.trades, pf.trades)
        self.assertEqual(loaded.next_id, 3)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["account"], {"initial_capital": 10_000_000, "losscut_level": 50.0})
        self.assertFalse(self.path.with_name("portfolio.json.tmp").exists())

    def test_version1_file_is_loaded_with_defaults(self):
        # レバレッジと口座の設定が無い、最初の版のデータ
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "next_id": 3,
                    "positions": [
                        {"id": 2, "symbol": "KIOXIA", "side": "long", "qty": 100, "entry_price": 3000.0,
                         "opened_at": "2026-11-02"}
                    ],
                    "trades": [
                        {"position_id": 1, "symbol": "USDJPY", "side": "short", "qty": 10000, "entry_price": 150.0,
                         "exit_price": 149.0, "opened_at": "2026-10-01", "closed_at": "2026-10-02"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        pf = Portfolio.load(self.path)
        self.assertEqual(pf.positions[0].leverage, 1.0)
        self.assertEqual((pf.trades[0].leverage, pf.trades[0].reason), (1.0, ""))
        # 読み込んだとたんにロスカットされないよう、ロスカットはオフで始める
        self.assertEqual((pf.initial_capital, pf.losscut_level), (10_000_000, 0.0))

    def test_account_settings_round_trip(self):
        pf = Portfolio()
        pf.set_account(initial_capital=3_000_000, losscut_level=0)
        pf.save(self.path)
        loaded = Portfolio.load(self.path)
        self.assertEqual((loaded.initial_capital, loaded.losscut_level), (3_000_000, 0))

    def test_new_file_has_default_losscut(self):
        self.assertEqual(Portfolio.load(self.path).losscut_level, 50.0)

    def test_corrupt_file_raises(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PortfolioError):
            Portfolio.load(self.path)


if __name__ == "__main__":
    unittest.main()
