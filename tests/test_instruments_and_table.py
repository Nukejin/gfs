import unittest

from trade_sim.instruments import UnknownInstrumentError, find_instrument, get_instrument
from trade_sim.table import render_table, text_width


class FindInstrumentTest(unittest.TestCase):
    def test_lookup_variants(self):
        cases = {
            "kioxia": "KIOXIA",
            "キオクシア": "KIOXIA",
            "285A": "KIOXIA",
            "285a.t": "KIOXIA",
            "LaserTec": "LASERTEC",
            "6920": "LASERTEC",
            "レーザーテック": "LASERTEC",
            "usdjpy": "USDJPY",
            "USD/JPY": "USDJPY",
            "ドル円": "USDJPY",
            "ＵＳＤＪＰＹ": "USDJPY",  # 全角
            "ﾄﾞﾙ円": "USDJPY",  # 半角カナ
            "eurjpy=x": "EURJPY",
            "ユーロ円": "EURJPY",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_instrument(text).key, key)

    def test_unknown_instrument(self):
        with self.assertRaises(UnknownInstrumentError) as cm:
            find_instrument("toyota")
        self.assertIn("キオクシア", str(cm.exception))

    def test_get_instrument(self):
        self.assertEqual(get_instrument("EURJPY").ticker, "EURJPY=X")
        with self.assertRaises(UnknownInstrumentError):
            get_instrument("XXX")

    def test_tickers(self):
        self.assertEqual(find_instrument("kioxia").ticker, "285A.T")
        self.assertEqual(find_instrument("lasertec").ticker, "6920.T")


class TableTest(unittest.TestCase):
    def test_text_width_counts_fullwidth_as_two(self):
        self.assertEqual(text_width("abc"), 3)
        self.assertEqual(text_width("ドル円"), 6)
        self.assertEqual(text_width("+1,000円"), 8)

    def test_columns_are_aligned(self):
        table = render_table(["銘柄", "損益"], [["ドル円", "+1円"], ["レーザーテック", "-10,000円"]], "<>")
        widths = {text_width(line) for line in table.splitlines()}
        self.assertEqual(len(widths), 1, table)


if __name__ == "__main__":
    unittest.main()
