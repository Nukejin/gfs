"""建玉と決済履歴の管理、損益と証拠金の計算。

損益はすべて円で計算する (対象銘柄はすべて円建てのため)。
  ロング:   (決済値 - 建値) × 数量
  ショート: (建値 - 決済値) × 数量

レバレッジは、建てるのに必要な証拠金を決める (損益そのものは変わらない)。
  必要証拠金     = 建値 × 数量 ÷ レバレッジ
  資金           = 初期資金 + 実現損益
  有効証拠金     = 資金 + 評価損益
  余力           = 有効証拠金 - 必要証拠金
  証拠金維持率   = 有効証拠金 ÷ 必要証拠金 × 100 (%)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .instruments import INSTRUMENTS, format_leverage, get_instrument

LONG = "long"
SHORT = "short"
SIDE_LABELS = {LONG: "ロング", SHORT: "ショート"}
DATA_VERSION = 2

DEFAULT_CAPITAL = 10_000_000  # 初期資金 (円)
DEFAULT_LOSSCUT_LEVEL = 50.0  # 証拠金維持率 (%) がこれを下回ったらロスカット。0 ならロスカットしない
LOSSCUT = "losscut"  # Trade.reason: ロスカットによる決済


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
    leverage: float = 1.0

    def unrealized_pnl(self, price: float) -> float:
        return calc_pnl(self.side, self.entry_price, price, self.qty)

    @property
    def required_margin(self) -> float:
        return required_margin(self.entry_price, self.qty, self.leverage)


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
    leverage: float = 1.0
    reason: str = ""  # "losscut" ならロスカットによる決済

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


@dataclass
class AccountStatus:
    """口座の状態。"""

    initial_capital: float
    realized: float
    unrealized: float | None  # 現在値を取得できない建玉があれば None
    used_margin: float  # 必要証拠金の合計
    losscut_level: float

    @property
    def cash(self) -> float:
        """資金 (初期資金 + 実現損益)。"""
        return round(self.initial_capital + self.realized, 2)

    @property
    def equity(self) -> float | None:
        """有効証拠金 (資金 + 評価損益)。"""
        return None if self.unrealized is None else round(self.cash + self.unrealized, 2)

    @property
    def free_margin(self) -> float | None:
        """余力 (新しく建てるのに使える証拠金)。"""
        return None if self.equity is None else round(self.equity - self.used_margin, 2)

    @property
    def margin_level(self) -> float | None:
        """証拠金維持率 (%)。建玉が無ければ None。"""
        if self.equity is None or self.used_margin <= 0:
            return None
        return self.equity / self.used_margin * 100

    @property
    def losscut_triggered(self) -> bool:
        level = self.margin_level
        return self.losscut_level > 0 and level is not None and level < self.losscut_level


def required_margin(price: float, qty: int, leverage: float) -> float:
    return round(price * qty / leverage, 2)


def _check_leverage(leverage: object, symbol: str) -> None:
    inst = get_instrument(symbol)
    if isinstance(leverage, bool) or not isinstance(leverage, (int, float)) or not 1 <= leverage <= inst.max_leverage:
        raise PortfolioError(
            f"{inst.name}のレバレッジは 1 倍から {format_leverage(inst.max_leverage)}までで指定してください: {leverage}"
        )


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
        initial_capital: float = DEFAULT_CAPITAL,
        losscut_level: float = DEFAULT_LOSSCUT_LEVEL,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.trades = trades if trades is not None else []
        self.next_id = next_id
        self.initial_capital = initial_capital
        self.losscut_level = losscut_level

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
            if "account" in data:
                initial_capital = float(data["account"]["initial_capital"])
                losscut_level = float(data["account"]["losscut_level"])
            else:
                # レバレッジ導入前 (バージョン 1) のデータ。資金を考えずに建てた建玉があり得るので、
                # 読み込んだとたんにロスカットされないよう、ロスカットはオフで始める
                initial_capital, losscut_level = DEFAULT_CAPITAL, 0.0
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            raise PortfolioError(f"データファイル {path} を読み込めません: {e}") from e
        return cls(positions, trades, next_id, initial_capital, losscut_level)

    def save(self, path: str | os.PathLike) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": DATA_VERSION,
            "next_id": self.next_id,
            "account": {"initial_capital": self.initial_capital, "losscut_level": self.losscut_level},
            "positions": [asdict(p) for p in self.positions],
            "trades": [asdict(t) for t in self.trades],
        }
        # 書き込み途中で中断してもデータが壊れないよう、一時ファイルに書いてから置き換える
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ---- 取引 ----------------------------------------------------------

    def open(self, symbol: str, side: str, qty: int, price: float, at: str, leverage: float = 1.0) -> Position:
        """新規に建玉を作る (証拠金の余力は呼び出し側で確認する)。"""
        get_instrument(symbol)
        if side not in SIDE_LABELS:
            raise PortfolioError(f"売買区分が不正です: {side}")
        _check_qty(qty)
        _check_price(price)
        _check_leverage(leverage, symbol)
        position = Position(self.next_id, symbol, side, qty, price, at, float(leverage))
        self.next_id += 1
        self.positions.append(position)
        return position

    def find(self, position_id: int) -> Position:
        for position in self.positions:
            if position.id == position_id:
                return position
        raise PortfolioError(f"建玉 #{position_id} は見つかりません (保有中の建玉は positions で確認できます)。")

    def close(self, position_id: int, price: float, at: str, qty: int | None = None, reason: str = "") -> Trade:
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
            position.leverage,
            reason,
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

    def set_account(self, initial_capital: float | None = None, losscut_level: float | None = None) -> None:
        if initial_capital is not None:
            if not initial_capital > 0:
                raise PortfolioError(f"初期資金は 0 より大きい金額にしてください: {initial_capital}")
            self.initial_capital = float(initial_capital)
        if losscut_level is not None:
            if not 0 <= losscut_level <= 100:
                raise PortfolioError(f"ロスカット水準は 0〜100% で指定してください (0 ならロスカットしない): {losscut_level}")
            self.losscut_level = float(losscut_level)

    def status(self, prices: dict[str, float | None]) -> AccountStatus:
        """口座の状態。prices は銘柄 ID -> 現在値 (保有中の銘柄の分)。"""
        unrealized: float | None = 0.0
        for position in self.positions:
            price = prices.get(position.symbol)
            if price is None:
                unrealized = None
                break
            unrealized += position.unrealized_pnl(price)
        return AccountStatus(
            initial_capital=self.initial_capital,
            realized=self.realized_pnl,
            unrealized=None if unrealized is None else round(unrealized, 2),
            used_margin=round(sum(p.required_margin for p in self.positions), 2),
            losscut_level=self.losscut_level,
        )

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
