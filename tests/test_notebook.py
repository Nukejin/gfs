"""Colab 用の取引画面のテスト。ボタンを押す操作を、ウィジェットを直接操作して確かめる。"""

import contextlib
import io
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from trade_sim.portfolio import Portfolio
from trade_sim.simulator import Simulator

from .fakes import FakeSource

try:
    import ipywidgets  # noqa: F401

    from trade_sim.notebook import TradeApp, show
except ImportError:  # pragma: no cover
    TradeApp = None

NOW = datetime(2026, 11, 3, 10, 0)


@unittest.skipIf(TradeApp is None, "ipywidgets が必要")
class TradeAppTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "portfolio.json"
        self.source = FakeSource(
            latest={"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0},
            history={"LASERTEC": {date(2026, 10, 30): 19500.0}},
        )
        self.app = TradeApp(Simulator(self.path, self.source, clock=lambda: NOW))

    def test_initial_view(self):
        app = self.app
        self.assertIn("キオクシア", app.quote_view.value)
        self.assertIn("3,000.0", app.quote_view.value)
        self.assertIn("保有中の建玉はありません", app.positions_view.value)
        self.assertIn("通算損益: 0円", app.summary_view.value)
        self.assertIn("2026-11-03 10:00", app.status.value)
        self.assertTrue(app.close_button.disabled)
        self.assertFalse(app.order_button.disabled)

    def test_symbol_change_sets_default_quantity_and_unit(self):
        app = self.app
        app.symbol.value = "USDJPY"
        self.assertEqual((app.qty.value, app.unit.value), (10000, "通貨"))
        app.symbol.value = "LASERTEC"
        self.assertEqual((app.qty.value, app.unit.value), (100, "株"))

    def test_order_close_and_total_pnl(self):
        app = self.app
        app.order_button.click()  # キオクシア 100株 ロング @ 3,000
        self.assertIn("新規ロング: #1 キオクシア 100株", app.order_message.value)

        app.symbol.value = "USDJPY"
        app.side.value = "short"
        app.order_button.click()  # ドル円 1万通貨 ショート @ 150.000
        self.assertIn("新規ショート: #2 ドル円 10,000通貨", app.order_message.value)
        self.assertEqual([value for _, value in app.close_target.options], [1, 2])
        self.assertFalse(app.close_button.disabled)

        # 価格が動いてから「価格を更新」
        self.source.latest_prices.update(KIOXIA=3200.0, USDJPY=149.5)
        app.refresh_button.click()
        self.assertIn("+20,000円", app.positions_view.value)
        self.assertIn("+5,000円", app.positions_view.value)

        app.close_target.value = 1
        app.close_button.click()
        self.assertIn("損益 +20,000円", app.close_message.value)
        self.assertEqual([value for _, value in app.close_target.options], [2])
        self.assertIn("通算損益: +25,000円", app.summary_view.value)
        self.assertIn("#1", app.history_view.value)

    def test_partial_close(self):
        app = self.app
        app.order_button.click()
        app.close_qty.value = 40
        app.close_button.click()
        self.assertIn("40株", app.close_message.value)
        self.assertEqual(Portfolio.load(self.path).positions[0].qty, 60)

    def test_order_on_past_date(self):
        app = self.app
        app.symbol.value = "LASERTEC"
        app.order_date.value = "2026-11-01"
        app.order_button.click()
        self.assertIn("直前の営業日 2026-10-30 の終値", app.order_message.value)

    def test_errors_are_shown_on_screen(self):
        app = self.app
        app.order_date.value = "11月1日"
        app.order_button.click()
        self.assertIn("エラー", app.order_message.value)
        self.assertIn("YYYY-MM-DD", app.order_message.value)

        app.order_date.value = ""
        app.qty.value = 0
        app.order_button.click()
        self.assertIn("数量は 1 以上", app.order_message.value)
        self.assertEqual(Portfolio.load(self.path).positions, [])
        self.assertFalse(app.order_button.disabled)

    def test_unexpected_error_is_shown_and_buttons_are_enabled_again(self):
        app = self.app

        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        app.sim.open = broken
        app.order_button.click()
        self.assertIn("予期しないエラー", app.order_message.value)
        self.assertFalse(app.order_button.disabled)

    def test_close_all_needs_second_click(self):
        app = self.app
        app.order_button.click()
        app.symbol.value = "EURJPY"
        app.order_button.click()

        app.close_all_button.click()
        self.assertEqual(len(Portfolio.load(self.path).positions), 2)
        self.assertEqual(app.close_all_button.description, "もう一度押すと全決済")

        app.close_all_button.click()
        self.assertEqual(Portfolio.load(self.path).positions, [])
        self.assertEqual(app.close_all_button.description, "すべて決済")
        self.assertTrue(app.close_button.disabled)

    def test_close_all_confirmation_is_cancelled_by_other_actions(self):
        app = self.app
        app.order_button.click()
        app.close_all_button.click()
        app.order_button.click()  # 別の操作をすると確認は取り消される
        self.assertEqual(app.close_all_button.description, "すべて決済")
        app.close_all_button.click()
        self.assertEqual(len(Portfolio.load(self.path).positions), 2)

    def test_price_failure_is_shown(self):
        del self.source.latest_prices["EURJPY"]
        self.app.refresh_button.click()
        self.assertIn("ユーロ円 の価格を取得できませんでした", self.app.quote_view.value)

    def test_html_is_escaped(self):
        app = self.app
        app.order_date.value = "<b>x</b>"
        app.order_button.click()
        self.assertIn("&lt;b&gt;", app.order_message.value)

    def test_show_returns_app(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app = show(str(self.path), source=self.source)
        self.assertIsInstance(app, TradeApp)


if __name__ == "__main__":
    unittest.main()
