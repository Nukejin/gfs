"""テスト用の価格ソース (ネットワークに接続しない)。"""

from __future__ import annotations

from datetime import date

from trade_sim.instruments import Instrument
from trade_sim.prices import PriceError, Quote


class FakeSource:
    def __init__(self, latest=None, history=None, as_of=date(2026, 11, 2)):
        self.latest_prices: dict[str, float] = dict(latest or {})
        self.history: dict[str, dict[date, float]] = dict(history or {})
        self.as_of = as_of

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
