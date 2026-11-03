"""yfinance を使った価格の取得。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from .instruments import Instrument

# 指定日が休場日だったときに、直前の営業日を探してさかのぼる日数
LOOKBACK_DAYS = 14

# 最新の価格を使い回す秒数 (Colab のように開きっぱなしで使う場合も、これより古い価格は取得し直す)
LATEST_CACHE_SECONDS = 60


class PriceError(Exception):
    """価格を取得できなかった。"""


@dataclass(frozen=True)
class Quote:
    price: float
    as_of: date  # 価格の日付 (日足の日付)
    prev_close: float | None = None  # 前営業日の終値


class PriceSource(Protocol):
    def latest(self, inst: Instrument) -> Quote:
        """最新の価格 (取引時間中は現在値、時間外は直近の終値)。"""

    def on_date(self, inst: Instrument, day: date) -> Quote:
        """指定日の終値。休場日なら直前の営業日の終値。"""


class YFinanceSource:
    """Yahoo Finance の日足データを yfinance で取得する。

    日足の最後の行は取引時間中なら当日の途中経過 (現在値) になる。
    東証銘柄は Yahoo Finance 側でおよそ 20 分遅れのデータになる。
    """

    def __init__(self, cache_seconds: float = LATEST_CACHE_SECONDS) -> None:
        self.cache_seconds = cache_seconds
        self._latest_cache: dict[str, tuple[float, Quote]] = {}

    def clear_cache(self) -> None:
        self._latest_cache.clear()

    def latest(self, inst: Instrument) -> Quote:
        cached = self._latest_cache.get(inst.key)
        if cached is not None and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        closes = self._daily_closes(inst, period="1mo")
        if not closes:
            raise PriceError(f"{inst.name}({inst.ticker}) の価格データを取得できませんでした。")
        day, price = closes[-1]
        prev_close = closes[-2][1] if len(closes) >= 2 else None
        quote = Quote(price, day, prev_close)
        self._latest_cache[inst.key] = (time.monotonic(), quote)
        return quote

    def on_date(self, inst: Instrument, day: date) -> Quote:
        start = day - timedelta(days=LOOKBACK_DAYS)
        end = day + timedelta(days=1)  # yfinance の end はその日を含まない
        closes = [
            (d, p)
            for d, p in self._daily_closes(inst, start=start.isoformat(), end=end.isoformat())
            if d <= day
        ]
        if not closes:
            raise PriceError(
                f"{day} 以前 {LOOKBACK_DAYS} 日間に {inst.name}({inst.ticker}) の価格データがありません。"
            )
        found_day, price = closes[-1]
        return Quote(price, found_day)

    def _daily_closes(self, inst: Instrument, **kwargs) -> list[tuple[date, float]]:
        try:
            import yfinance as yf
        except ImportError as e:
            raise PriceError(
                "yfinance がインストールされていません。`pip install -r requirements.txt` を実行してください。"
            ) from e

        # データが無いときの yfinance 自身のログは、こちらのエラーメッセージで代わりに伝える
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        try:
            # auto_adjust=False: 配当で調整されていない、実際の終値を使う
            df = yf.Ticker(inst.ticker).history(interval="1d", auto_adjust=False, actions=False, **kwargs)
        except Exception as e:  # 通信エラーやレート制限など、yfinance は色々な例外を投げる
            raise PriceError(f"{inst.name}({inst.ticker}) の価格取得に失敗しました: {e}") from e

        if df is None or df.empty or "Close" not in df.columns:
            return []
        closes = df["Close"].dropna()
        return [(ts.date(), round(float(value), inst.price_decimals)) for ts, value in closes.items()]
