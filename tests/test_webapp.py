"""ブラウザ版 (app.py / trade_sim/webapp.py) のテスト。Streamlit の AppTest で画面を操作する。"""

import tempfile
import unittest
from pathlib import Path

from trade_sim.portfolio import Portfolio

from .fakes import FakeSource

try:
    import plotly  # noqa: F401
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PRICES = {"KIOXIA": 3000.0, "LASERTEC": 20000.0, "USDJPY": 150.0, "EURJPY": 160.0}


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
        return [m.value for m in self.at.markdown if "white-space: nowrap" in m.value]

    def test_initial_screen(self):
        self.assertEqual([m.label for m in self.at.metric], ["キオクシア", "レーザーテック", "ドル円", "ユーロ円"])
        self.assertEqual(self.at.metric[0].value, "3,000.0")
        self.assertIn("0円", self.kpis()[0])
        self.assertEqual(len(self.at.get("plotly_chart")), 1)  # ローソク足チャート

    def test_order_close_and_total_pnl(self):
        self.click("発注する")  # キオクシア 100株 ロング @ 3,000
        self.assertIn("新規ロング: #1 キオクシア 100株", self.at.success[0].value)

        self.at.selectbox(key="order_symbol").select("USDJPY").run()
        self.assertEqual(self.at.number_input(key="order_qty").value, 10000)  # 為替は 1 万通貨が初期値
        self.at.radio(key="order_side").set_value("short").run()
        self.click("発注する")
        self.assertIn("新規ショート: #2 ドル円 10,000通貨", self.at.success[0].value)

        self.source.latest_prices.update(KIOXIA=3200.0, USDJPY=149.5)
        self.click("価格を今すぐ更新")
        self.assertIn("+25,000円", self.kpis()[0])  # 通算損益 = 評価損益 +20,000 +5,000

        self.at.selectbox(key="close_id").select(1).run()
        self.click("決済する")
        self.assertIn("損益 +20,000円", self.at.success[0].value)
        kpis = self.kpis()
        self.assertIn("+25,000円", kpis[0])  # 通算
        self.assertIn("+20,000円", kpis[1])  # 実現
        self.assertIn("+5,000円", kpis[2])  # 評価
        self.assertEqual(len(Portfolio.load(self.path).trades), 1)
        self.assertEqual(len(self.at.get("plotly_chart")), 3)  # ローソク足、損益の推移、銘柄別の損益

    def test_partial_close(self):
        self.click("発注する")
        self.at.number_input(key="close_qty").set_value(40).run()
        self.click("決済する")
        self.assertIn("40株", self.at.success[0].value)
        self.assertEqual(Portfolio.load(self.path).positions[0].qty, 60)
        self.assertEqual(self.at.number_input(key="close_qty").value, 0)  # 決済したら 0 (すべて) に戻る

    def test_close_all(self):
        self.click("発注する")
        self.at.selectbox(key="order_symbol").select("EURJPY").run()
        self.click("発注する")
        self.click(key="close_all_confirm")
        self.assertEqual(Portfolio.load(self.path).positions, [])
        self.assertIn("実現損益の通算", self.at.success[0].value)

    def test_price_error_is_shown(self):
        del self.source.latest_prices["KIOXIA"]
        self.click("価格を今すぐ更新")
        self.click("発注する")
        self.assertIn("キオクシア の価格を取得できませんでした", self.at.error[0].value)
        self.assertEqual(Portfolio.load(self.path).positions, [])

    def test_history_has_csv_download(self):
        self.click("発注する")
        self.click("決済する")
        self.assertEqual(len(self.at.get("download_button")), 1)


if __name__ == "__main__":
    unittest.main()
