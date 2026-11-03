"""ブラウザ版 (app.py / trade_sim/webapp.py) のテスト。Streamlit の AppTest で画面を操作する。"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from trade_sim.portfolio import LOSSCUT, Portfolio

from .fakes import FakeSource

try:
    import plotly  # noqa: F401
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PRICES = {"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0}
LONG_BUTTON = "ロング（買い）で発注"
SHORT_BUTTON = "ショート（売り）で発注"


@unittest.skipIf(AppTest is None, "streamlit と plotly が必要")
class WebAppTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "portfolio.json"
        self.source = FakeSource(latest=dict(PRICES))
        self.at = AppTest.from_file(APP, default_timeout=60)
        self.at.session_state["price_source"] = self.source
        self.at.session_state["data_file"] = str(self.path)
        self.at.run()
        self.assertNoException()

    def assertNoException(self):
        self.assertEqual([e.value for e in self.at.exception], [])

    def click(self, label=None, key=None):
        button = self.at.button(key=key) if key else next(b for b in self.at.button if b.label == label)
        button.click().run()
        self.assertNoException()

    def kpis(self):
        """上段のカード (有効証拠金・通算損益・余力・証拠金維持率) の値。"""
        values = [re.sub(r"<[^>]+>", "", m.value) for m in self.at.markdown if "white-space: nowrap" in m.value]
        return dict(zip(["有効証拠金", "通算損益", "余力", "証拠金維持率"], values[:4]))

    def order(self, symbol="KIOXIA", side="long", qty=None, leverage=None):
        kind = "stock" if symbol in ("KIOXIA", "LASERTEC") else "fx"
        self.at.selectbox(key="symbol").select(symbol).run()
        self.at.segmented_control(key="order_side").set_value(side).run()
        if qty is not None:
            self.at.number_input(key=f"order_qty_{kind}").set_value(qty).run()
        if leverage is not None:
            self.at.select_slider(key=f"order_leverage_{kind}").set_value(leverage).run()
        self.click(LONG_BUTTON if side == "long" else SHORT_BUTTON)

    def test_initial_screen(self):
        self.assertEqual(
            self.kpis(), {"有効証拠金": "10,000,000円", "通算損益": "0円", "余力": "10,000,000円", "証拠金維持率": "-"}
        )
        self.assertEqual([m.label for m in self.at.metric][:4], ["キオクシア", "レーザーテック", "ドル円", "ユーロ円"])
        self.assertEqual(len(self.at.get("plotly_chart")), 1)  # チャートと注文パネルが同じタブにある
        self.assertEqual(self.at.select_slider(key="order_leverage_stock").value, 1.0)

    def test_order_panel_follows_the_chart_symbol(self):
        self.at.selectbox(key="symbol").select("USDJPY").run()
        self.assertEqual(self.at.number_input(key="order_qty_fx").value, 10000)  # 為替は 1 万通貨が初期値
        self.assertEqual(self.at.select_slider(key="order_leverage_fx").label, "レバレッジ（25倍まで）")
        self.click(LONG_BUTTON)
        self.assertIn("新規ロング: #1 ドル円 10,000通貨", self.at.success[0].value)

    def test_order_with_leverage_and_close(self):
        self.order("KIOXIA", "long", qty=1000, leverage=3.0)  # 取引金額 300 万円、必要証拠金 100 万円
        self.assertIn("レバレッジ 3倍・必要証拠金 1,000,000円", self.at.success[0].value)
        self.assertEqual(self.kpis()["余力"], "9,000,000円")
        self.assertEqual(self.kpis()["証拠金維持率"], "1,000.0%")

        self.order("USDJPY", "short")
        self.source.latest_prices.update(KIOXIA=3200.0, USDJPY=149.5)
        self.click("価格を今すぐ更新")
        self.assertEqual(self.kpis()["通算損益"], "+205,000円")  # +200,000 +5,000

        self.at.selectbox(key="close_id").select(1).run()
        self.click("決済する")
        self.assertIn("損益 +200,000円", self.at.success[0].value)
        self.assertEqual(self.kpis()["有効証拠金"], "10,205,000円")
        self.assertEqual(len(Portfolio.load(self.path).trades), 1)
        self.assertEqual(len(self.at.get("plotly_chart")), 3)  # ローソク足、損益の推移、銘柄別の損益

    def test_order_larger_than_free_margin_is_rejected(self):
        self.order("LASERTEC", "long", qty=2000, leverage=3.3)
        self.assertIn("余力が足りません", self.at.error[0].value)
        self.assertEqual(Portfolio.load(self.path).positions, [])

    def test_losscut(self):
        self.order("USDJPY", "long", qty=100000, leverage=25.0)  # 必要証拠金 60 万円
        self.at.number_input(key="account_capital").set_value(1_000_000).run()
        self.assertNoException()
        self.source.latest_prices["USDJPY"] = 142.0  # 有効証拠金 20 万円 → 維持率 33%
        self.click("価格を今すぐ更新")
        self.assertIn("ロスカット", self.at.error[0].value)
        portfolio = Portfolio.load(self.path)
        self.assertEqual(portfolio.positions, [])
        self.assertEqual(portfolio.trades[0].reason, LOSSCUT)

    def test_account_settings_are_saved(self):
        self.at.number_input(key="account_capital").set_value(3_000_000).run()
        self.at.number_input(key="account_losscut").set_value(0.0).run()
        self.assertNoException()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["account"], {"initial_capital": 3_000_000.0, "losscut_level": 0.0})
        self.assertEqual(self.kpis()["有効証拠金"], "3,000,000円")

    def test_partial_close(self):
        self.order("KIOXIA", "long")
        self.at.number_input(key="close_qty").set_value(40).run()
        self.click("決済する")
        self.assertIn("40株", self.at.success[0].value)
        self.assertEqual(Portfolio.load(self.path).positions[0].qty, 60)
        self.assertEqual(self.at.number_input(key="close_qty").value, 0)  # 決済したら 0 (すべて) に戻る

    def test_close_all(self):
        self.order("KIOXIA", "long")
        self.order("EURJPY", "short")
        self.click(key="close_all_confirm")
        self.assertEqual(Portfolio.load(self.path).positions, [])
        self.assertIn("実現損益の通算", self.at.success[0].value)

    def test_price_error_disables_the_order_button(self):
        del self.source.latest_prices["KIOXIA"]
        self.click("価格を今すぐ更新")
        self.assertTrue(next(b for b in self.at.button if b.label == LONG_BUTTON).disabled)

    def test_history_has_csv_download(self):
        self.order("KIOXIA", "long")
        self.click("決済する")
        self.assertEqual(len(self.at.get("download_button")), 1)


if __name__ == "__main__":
    unittest.main()
