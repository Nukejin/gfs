"""建玉と決済履歴の管理、損益の計算。

損益はすべて円で計算する (対象銘柄はすべて円建てのため)。
  ロング:   (決済値 - 建値) × 数量
  ショート: (建値 - 決済値) × 数量
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .instruments import INSTRUMENTS, get_instrument

LONG = "long"
SHORT = "short"
SIDE_LABELS = {LONG: "ロング", SHORT: "ショート"}
DATA_VERSION = 1


class PortfolioError(Exception):
    pass


def calc_pnl(side: str, entry_price: float, exit_price: float, qty: int) -> float:
    diff = exit_price - entry_price if side == LONG else entry_price - exit_price
    # 浮動小数点の誤差 (例: 22220.000000000004) を落とすため銭単位で丸める
    return round(diff * qty, 2)


@dataclass
class Position:
    """保有中の建玉。"""

    id: int
    symbol: str
    side: str
    qty: int
    entry_price: float
    opened_at: str  # "YYYY-MM-DD HH:MM" (現在値で約定) または "YYYY-MM-DD" (終値で約定)

    def unrealized_pnl(self, price: float) -> float:
        return calc_pnl(self.side, self.entry_price, price, self.qty)


@dataclass
class Trade:
    """決済済みの取引。一部決済した場合は決済ごとに 1 件。"""

    position_id: int
    symbol: str
    side: str
    qty: int
    entry_price: float
    exit_price: float
    opened_at: str
    closed_at: str

    @property
    def pnl(self) -> float:
        return calc_pnl(self.side, self.entry_price, self.exit_price, self.qty)


@dataclass
class SummaryRow:
    """銘柄ごとの損益集計。"""

    symbol: str
    trades: int  # 決済回数
    wins: int  # 利益が出た決済の回数
    realized: float  # 実現損益
    open_positions: int  # 保有中の建玉数
    unrealized: float | None  # 評価損益 (現在値が取得できなかった場合は None)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def total(self) -> float | None:
        return None if self.unrealized is None else round(self.realized + self.unrealized, 2)


def _check_qty(qty: object) -> None:
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise PortfolioError(f"数量は 1 以上の整数で指定してください: {qty}")


def _check_price(price: float) -> None:
    if not price > 0:
        raise PortfolioError(f"価格が不正です: {price}")


class Portfolio:
    def __init__(
        self,
        positions: list[Position] | None = None,
        trades: list[Trade] | None = None,
        next_id: int = 1,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.trades = trades if trades is not None else []
        self.next_id = next_id

    # ---- 保存・読み込み ------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike) -> Portfolio:
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            positions = [Position(**p) for p in data.get("positions", [])]
            trades = [Trade(**t) for t in data.get("trades", [])]
            next_id = int(data.get("next_id", 1))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            raise PortfolioError(f"データファイル {path} を読み込めません: {e}") from e
        return cls(positions, trades, next_id)

    def save(self, path: str | os.PathLike) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": DATA_VERSION,
            "next_id": self.next_id,
            "positions": [asdict(p) for p in self.positions],
            "trades": [asdict(t) for t in self.trades],
        }
        # 書き込み途中で中断してもデータが壊れないよう、一時ファイルに書いてから置き換える
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ---- 取引 ----------------------------------------------------------

    def open(self, symbol: str, side: str, qty: int, price: float, at: str) -> Position:
        """新規に建玉を作る。"""
        get_instrument(symbol)
        if side not in SIDE_LABELS:
            raise PortfolioError(f"売買区分が不正です: {side}")
        _check_qty(qty)
        _check_price(price)
        position = Position(self.next_id, symbol, side, qty, price, at)
        self.next_id += 1
        self.positions.append(position)
        return position

    def find(self, position_id: int) -> Position:
        for position in self.positions:
            if position.id == position_id:
                return position
        raise PortfolioError(f"建玉 #{position_id} は見つかりません (保有中の建玉は positions で確認できます)。")

    def close(self, position_id: int, price: float, at: str, qty: int | None = None) -> Trade:
        """建玉を決済する。qty を指定すると一部だけ決済する。"""
        position = self.find(position_id)
        qty = position.qty if qty is None else qty
        _check_qty(qty)
        _check_price(price)
        if qty > position.qty:
            raise PortfolioError(f"決済数量 {qty:,} が建玉 #{position_id} の数量 {position.qty:,} を超えています。")
        if at[:10] < position.opened_at[:10]:
            raise PortfolioError(
                f"決済日 {at[:10]} が建玉 #{position_id} の約定日 {position.opened_at[:10]} より前です。"
            )

        trade = Trade(
            position.id,
            position.symbol,
            position.side,
            qty,
            position.entry_price,
            price,
            position.opened_at,
            at,
        )
        self.trades.append(trade)
        position.qty -= qty
        if position.qty == 0:
            self.positions.remove(position)
        return trade

    # ---- 集計 ----------------------------------------------------------

    @property
    def realized_pnl(self) -> float:
        return round(sum(t.pnl for t in self.trades), 2)

    def summary(self, prices: dict[str, float | None]) -> list[SummaryRow]:
        """銘柄ごとの損益。prices は銘柄 ID -> 現在値 (保有中の銘柄の分だけあればよい)。"""
        rows = []
        for inst in INSTRUMENTS:
            trades = [t for t in self.trades if t.symbol == inst.key]
            positions = [p for p in self.positions if p.symbol == inst.key]
            price = prices.get(inst.key)
            if not positions:
                unrealized: float | None = 0.0
            elif price is None:
                unrealized = None
            else:
                unrealized = round(sum(p.unrealized_pnl(price) for p in positions), 2)
            rows.append(
                SummaryRow(
                    symbol=inst.key,
                    trades=len(trades),
                    wins=sum(1 for t in trades if t.pnl > 0),
                    realized=round(sum(t.pnl for t in trades), 2),
                    open_positions=len(positions),
                    unrealized=unrealized,
                )
            )
        return rows
