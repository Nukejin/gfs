"""yfinance を使った価格の取得。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from .instruments import Instrument

if TYPE_CHECKING:
    import pandas as pd

# 指定日が休場日だったときに、直前の営業日を探してさかのぼる日数
LOOKBACK_DAYS = 14

# 最新の価格を使い回す秒数 (Colab のように開きっぱなしで使う場合も、これより古い価格は取得し直す)
LATEST_CACHE_SECONDS = 60


class PriceError(Exception):
    """価格を取得できなかった。"""


# yfinance の period を短い順に並べたもの
PERIOD_ORDER = ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max")


@dataclass(frozen=True)
class Timeframe:
    """チャートの足の種類。"""

    key: str
    label: str
    interval: str  # yfinance の interval
    periods: tuple[tuple[str, str], ...]  # (表示名, yfinance の period)
    default_period: str
    ma_windows: tuple[int, ...]  # 移動平均線の本数
    intraday: bool
    max_fetch: str  # 取得できる最長の period (Yahoo の制限: 5分足・15分足は 60 日、1時間足は 730 日まで)

    def fetch_period(self, period: str) -> str:
        """移動平均線をチャートの左端から描けるよう、表示する期間より 1 段階長く取得する。"""
        longer = PERIOD_ORDER[min(PERIOD_ORDER.index(period) + 1, len(PERIOD_ORDER) - 1)]
        return longer if PERIOD_ORDER.index(longer) <= PERIOD_ORDER.index(self.max_fetch) else period


TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe("5m", "5分足", "5m", (("1日", "1d"), ("5日", "5d"), ("1か月", "1mo")), "5d", (5, 25, 75), True, "1mo"),
    Timeframe("15m", "15分足", "15m", (("5日", "5d"), ("1か月", "1mo")), "1mo", (5, 25, 75), True, "1mo"),
    Timeframe(
        "1h", "1時間足", "1h", (("1か月", "1mo"), ("3か月", "3mo"), ("6か月", "6mo"), ("1年", "1y")), "3mo",
        (5, 25, 75), True, "2y",
    ),
    Timeframe(
        "1d", "日足", "1d", (("3か月", "3mo"), ("6か月", "6mo"), ("1年", "1y"), ("2年", "2y"), ("5年", "5y")), "6mo",
        (5, 25, 75), False, "max",
    ),
    Timeframe(
        "1wk", "週足", "1wk", (("1年", "1y"), ("2年", "2y"), ("5年", "5y"), ("10年", "10y"), ("全期間", "max")), "2y",
        (13, 26, 52), False, "max",
    ),
)


def get_timeframe(key: str) -> Timeframe:
    for timeframe in TIMEFRAMES:
        if timeframe.key == key:
            return timeframe
    raise ValueError(f"未対応の足です: {key}")


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
        self._candle_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
        self._path_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}

    def clear_cache(self) -> None:
        self._latest_cache.clear()
        self._candle_cache.clear()
        self._path_cache.clear()

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

    def candles(self, inst: Instrument, timeframe: Timeframe, period: str) -> pd.DataFrame:
        """ローソク足 (Open/High/Low/Close/Volume)。

        移動平均線を計算できるよう、period より長めに取得する (表示する範囲は charts.visible_start で決める)。
        時刻はすべて日本時間 (タイムゾーン情報なし) にそろえる。日足・週足は日付 (0 時) にする。
        """
        fetch = timeframe.fetch_period(period)
        key = (inst.key, timeframe.key, fetch)
        cached = self._candle_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        import pandas as pd

        df = self._history(inst, interval=timeframe.interval, period=fetch)
        columns = ["Open", "High", "Low", "Close", "Volume"]
        if df is None or df.empty or not set(columns) <= set(df.columns):
            raise PriceError(f"{inst.name}({inst.ticker}) の{timeframe.label}のデータがありません。期間を変えてみてください。")
        df = df[columns].dropna(subset=["Open", "High", "Low", "Close"]).copy()
        if timeframe.intraday:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
        else:
            # 為替の日足はロンドン時間の 0 時なので、日本時間に直さずにその日付を使う
            df.index = pd.DatetimeIndex([pd.Timestamp(ts.date()) for ts in df.index])
        df = df[~df.index.duplicated(keep="last")]
        df["Volume"] = df["Volume"].fillna(0)
        if df.empty:
            raise PriceError(f"{inst.name}({inst.ticker}) の{timeframe.label}のデータがありません。期間を変えてみてください。")
        self._candle_cache[key] = (time.monotonic(), df)
        return df

    def path(self, inst: Instrument, since: datetime) -> pd.DataFrame:
        """since (日本時間) 以降に始まるローソク足 (Open/High/Low/Close)。決済注文の判定に使う。

        Yahoo の制限があるので、直近 58 日は 5 分足、720 日までは 1 時間足、それより前は日足を使う。
        使った足の種類は df.attrs["interval"] に入る。データが無ければ空の DataFrame を返す。
        """
        import pandas as pd

        now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)  # 日本時間
        age = now - since
        interval = "5m" if age <= timedelta(days=58) else "1h" if age <= timedelta(days=720) else "1d"
        start = (since - timedelta(days=1)).date().isoformat()
        key = (inst.key, interval, start)
        cached = self._path_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self.cache_seconds:
            df = cached[1]
        else:
            end = (now + timedelta(days=2)).date().isoformat()
            df = self._history(inst, interval=interval, start=start, end=end)
            columns = ["Open", "High", "Low", "Close"]
            if df is None or df.empty or not set(columns) <= set(df.columns):
                df = pd.DataFrame(columns=columns, index=pd.DatetimeIndex([]))
            else:
                df = df[columns].dropna().copy()
                if interval == "1d":
                    df.index = pd.DatetimeIndex([pd.Timestamp(ts.date()) for ts in df.index])
                else:
                    df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
                df = df[~df.index.duplicated(keep="last")]
            self._path_cache[key] = (time.monotonic(), df)
        result = df[df.index >= pd.Timestamp(since)]
        result.attrs["interval"] = interval
        return result

    def _daily_closes(self, inst: Instrument, **kwargs) -> list[tuple[date, float]]:
        df = self._history(inst, interval="1d", **kwargs)
        if df is None or df.empty or "Close" not in df.columns:
            return []
        closes = df["Close"].dropna()
        return [(ts.date(), round(float(value), inst.price_decimals)) for ts, value in closes.items()]

    def _history(self, inst: Instrument, **kwargs) -> pd.DataFrame | None:
        try:
            import yfinance as yf
        except ImportError as e:
            raise PriceError(
                "yfinance がインストールされていません。`pip install -r requirements.txt` を実行してください。"
            ) from e

        # データが無いときの yfinance 自身のログは、こちらのエラーメッセージで代わりに伝える
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        try:
            # auto_adjust=False: 配当で調整されていない、実際の価格を使う
            return yf.Ticker(inst.ticker).history(auto_adjust=False, actions=False, **kwargs)
        except Exception as e:  # 通信エラーやレート制限など、yfinance は色々な例外を投げる
            raise PriceError(f"{inst.name}({inst.ticker}) の価格取得に失敗しました: {e}") from e
