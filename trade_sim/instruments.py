"""シミュレーション対象の銘柄の定義。

銘柄を増やすときは INSTRUMENTS に Instrument を追加する。
損益はすべて円で計算するので、円建ての銘柄 (東証の株、XXX/JPY の為替) を追加すること。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    key: str  # 内部 ID (保存データにはこの値が入る)
    name: str  # 表示名
    ticker: str  # yfinance (Yahoo Finance) のティッカー
    kind: str  # "stock" (株式) / "fx" (為替)
    unit: str  # 数量の単位
    price_decimals: int  # 価格の小数点以下の桁数
    aliases: tuple[str, ...] = ()  # コマンドで指定するときの別名

    def format_price(self, price: float) -> str:
        return f"{price:,.{self.price_decimals}f}"

    def format_qty(self, qty: int) -> str:
        return f"{qty:,}{self.unit}"


INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("KIOXIA", "キオクシア", "285A.T", "stock", "株", 1, ("285A",)),
    Instrument("LASERTEC", "レーザーテック", "6920.T", "stock", "株", 1, ("6920",)),
    Instrument("USDJPY", "ドル円", "USDJPY=X", "fx", "通貨", 3, ("USD/JPY", "ドル/円")),
    Instrument("EURJPY", "ユーロ円", "EURJPY=X", "fx", "通貨", 3, ("EUR/JPY", "ユーロ/円")),
)


class UnknownInstrumentError(ValueError):
    pass


def _normalize(text: str) -> str:
    # 全角英数字や半角カナでも指定できるように NFKC で正規化する
    return unicodedata.normalize("NFKC", text).strip().lower()


_BY_KEY = {inst.key: inst for inst in INSTRUMENTS}
_LOOKUP = {
    _normalize(name): inst
    for inst in INSTRUMENTS
    for name in (inst.key, inst.name, inst.ticker, *inst.aliases)
}


def get_instrument(key: str) -> Instrument:
    """保存データ内の ID から銘柄を引く。"""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise UnknownInstrumentError(f"未対応の銘柄 ID です: {key}") from None


def find_instrument(text: str) -> Instrument:
    """コマンドで入力された銘柄名・コード・別名から銘柄を探す。"""
    inst = _LOOKUP.get(_normalize(text))
    if inst is None:
        choices = "、".join(f"{i.name}({i.key.lower()})" for i in INSTRUMENTS)
        raise UnknownInstrumentError(f"銘柄「{text}」は対応していません。対応銘柄: {choices}")
    return inst
