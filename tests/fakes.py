"""テスト用の価格ソース (ネットワークに接続しない)。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from trade_sim.instruments import Instrument
from trade_sim.prices import PriceError, Quote, Timeframe


class FakeSource:
    def __init__(self, latest=None, history=None, as_of=date(2026, 11, 2)):
        self.latest_prices: dict[str, float] = dict(latest or {})
        self.history: dict[str, dict[date, float]] = dict(history or {})
        self.as_of = as_of
        # 決済注文の判定に使う値動き: 銘柄 ID -> [(時刻, 始値, 高値, 安値, 終値), ...]
        self.paths: dict[str, list[tuple[datetime, float, float, float, float]]] = {}
        self.path_interval = "5m"

    def latest(self, inst: Instrument) -> Quote:
        if inst.key not in self.latest_prices:
            raise PriceError(f"{inst.name} の価格を取得できませんでした (テスト)")
        return Quote(self.latest_prices[inst.key], self.as_of, None)

    def on_date(self, inst: Instrument, day: date) -> Quote:
        days = [d for d in self.history.get(inst.key, {}) if d <= day]
        if not days:
            raise PriceError(f"{day} 以前に {inst.name} の価格データがありません (テスト)")
        found = max(days)
        return Quote(self.history[inst.key][found], found)

    def candles(self, inst: Instrument, timeframe: Timeframe, period: str):
        """latest の価格で終わる 40 本のローソク足 (テスト用の決まった値)。"""
        import pandas as pd

        if inst.key not in self.latest_prices:
            raise PriceError(f"{inst.name} の{timeframe.label}のデータがありません (テスト)")
        last = self.latest_prices[inst.key]
        step = timedelta(minutes=5) if timeframe.intraday else timedelta(days=1)
        end = datetime(2026, 11, 2, 15, 0) if timeframe.intraday else datetime(2026, 11, 2)
        index = pd.DatetimeIndex([end - step * i for i in range(39, -1, -1)])
        closes = [last * (1 + 0.01 * ((i % 7) - 3) / 3) for i in range(39)] + [last]
        opens = [closes[0]] + closes[:-1]
        return pd.DataFrame(
            {
                "Open": opens,
                "High": [max(o, c) * 1.002 for o, c in zip(opens, closes)],
                "Low": [min(o, c) * 0.998 for o, c in zip(opens, closes)],
                "Close": closes,
                "Volume": [1000 * (i + 1) if inst.kind == "stock" else 0 for i in range(40)],
            },
            index=index,
        )

    def path(self, inst: Instrument, since: datetime):
        """paths に入れた値動きのうち、since 以降に始まるもの。入れていない銘柄は None (データなし)。"""
        if inst.key not in self.paths:
            return None
        import pandas as pd

        rows = [row for row in self.paths[inst.key] if row[0] >= since]
        df = pd.DataFrame(
            {name: [row[i] for row in rows] for i, name in enumerate(["Open", "High", "Low", "Close"], start=1)},
            index=pd.DatetimeIndex([row[0] for row in rows]),
        )
        df.attrs["interval"] = self.path_interval
        return df
