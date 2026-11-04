"""シミュレーターの操作。コマンドライン (cli.py) と Colab の画面 (notebook.py) の共通部分。

各操作は表示用の Result を返し、表示のしかたは呼び出し側が決める。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .instruments import (
    INSTRUMENTS,
    Instrument,
    UnknownInstrumentError,
    find_instrument,
    format_leverage,
    get_instrument,
)
from .portfolio import (
    LOSSCUT,
    REASON_LABELS,
    SIDE_LABELS,
    AccountStatus,
    Portfolio,
    PortfolioError,
    Position,
    SummaryRow,
    Trade,
    exit_prices,
    find_exit,
    parse_trade_time,
    required_margin,
)
from .prices import PriceError, PriceSource, YFinanceSource

# 日本に夏時間はないので、固定の +9 時間で日本時間になる
JST = timezone(timedelta(hours=9), "JST")


class InputError(Exception):
    """入力内容の誤り。"""


# 利用者に内容をそのまま伝えればよいエラー
USER_ERRORS = (InputError, PortfolioError, PriceError, UnknownInstrumentError)


def now_jst() -> datetime:
    """日本時間の現在時刻。Colab のように UTC で動く環境でも、取引時刻を日本時間で記録する。"""
    return datetime.now(JST).replace(tzinfo=None)


def parse_date(text: str | None) -> date | None:
    """"YYYY-MM-DD" を日付にする。空欄なら None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text.replace("/", "-"))
    except ValueError:
        raise InputError(f"日付は YYYY-MM-DD 形式で指定してください: {text}") from None


# ---- 表示用の整形 --------------------------------------------------------


def fmt_yen(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(value)
    return "0円" if rounded == 0 else f"{rounded:+,}円"


def fmt_amount(value: float | None) -> str:
    """符号を付けない金額 (資金・証拠金など)。"""
    return "-" if value is None else f"{round(value):,}円"


def fmt_level(level: float | None) -> str:
    """証拠金維持率。建玉が無ければ "-"。"""
    return "-" if level is None else f"{level:,.1f}%"


def fmt_rate(rate: float | None) -> str:
    return "-" if rate is None else f"{rate * 100:.0f}%"


def fmt_change(inst: Instrument, price: float, prev_close: float | None) -> str:
    if not prev_close:
        return "-"
    diff = price - prev_close
    return f"{diff:+,.{inst.price_decimals}f} ({diff / prev_close * 100:+.2f}%)"


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    aligns: str  # 列ごとの寄せ方 ("<" 左寄せ / ">" 右寄せ)
    footer: list[list[str]] = field(default_factory=list)


@dataclass
class Result:
    table: Table | None = None
    lines: list[str] = field(default_factory=list)  # 約定の結果や合計など
    notes: list[str] = field(default_factory=list)  # 注意書き (価格を取得できなかった等)
    ok: bool = True
    losscut: bool = False  # ロスカットが起きた


# ---- 操作 ----------------------------------------------------------------


class Simulator:
    def __init__(
        self,
        path: str | Path,
        source: PriceSource | None = None,
        clock: Callable[[], datetime] = now_jst,
    ) -> None:
        self.path = Path(path)
        self.source = source if source is not None else YFinanceSource()
        self.clock = clock

    def load(self) -> Portfolio:
        return Portfolio.load(self.path)

    def refresh_prices(self) -> None:
        """取得済みの価格を捨てて、次の操作で取得し直す。"""
        clear = getattr(self.source, "clear_cache", None)
        if clear is not None:
            clear()

    def quote(self, symbols: Iterable[str] = ()) -> Result:
        instruments = [find_instrument(s) for s in symbols] or list(INSTRUMENTS)
        rows, notes = [], []
        for inst in instruments:
            try:
                quote = self.source.latest(inst)
            except PriceError as e:
                rows.append([inst.name, inst.ticker, "-", "-", "-"])
                notes.append(str(e))
                continue
            rows.append(
                [
                    inst.name,
                    inst.ticker,
                    inst.format_price(quote.price),
                    fmt_change(inst, quote.price, quote.prev_close),
                    quote.as_of.isoformat(),
                ]
            )
        table = Table(["銘柄", "ティッカー", "現在値", "前日比", "日付"], rows, "<<>><")
        return Result(table=table, notes=notes, ok=len(notes) < len(instruments))

    def open(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float | None = None,
        day: date | None = None,
        leverage: float = 1.0,
        take_profit_yen: float | None = None,
        stop_loss_yen: float | None = None,
    ) -> Result:
        """新規にロング (side="long") またはショート (side="short") で建てる。

        必要証拠金 (取引金額 ÷ レバレッジ) が余力を超える注文はできない。
        take_profit_yen / stop_loss_yen (円) を渡すと、その金額の利益・損失になったら決済する注文も出す。
        """
        inst = find_instrument(symbol)
        if side not in SIDE_LABELS:
            raise InputError(f"売買区分が不正です: {side}")
        if isinstance(leverage, bool) or not isinstance(leverage, (int, float)) or not 1 <= leverage <= inst.max_leverage:
            raise InputError(
                f"{inst.name}のレバレッジは 1 倍から {format_leverage(inst.max_leverage)}までで指定してください: {leverage}"
            )
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise InputError(f"数量は 1 以上の整数で指定してください: {qty}")
        for amount in (take_profit_yen, stop_loss_yen):
            if amount is not None and amount < 0:
                raise InputError(f"利益確定・損切りの金額は 0 以上で指定してください (0 なら注文なし): {amount}")
        events = self.check_triggers()
        if events is not None and events.losscut:
            events.notes.append("ロスカットが発生したため、この注文は出していません。")
            events.ok = False
            return events
        event_lines = events.lines if events else []

        price, at, note = self._resolve_price(inst, price, day)
        portfolio = self.load()
        status = self._status(portfolio)
        margin = required_margin(price, qty, leverage)
        if status.free_margin is None:
            raise InputError("現在値を取得できない建玉があるため、余力を計算できません。少し待ってからもう一度試してください。")
        if margin > status.free_margin:
            raise InputError(
                f"余力が足りません。この注文の必要証拠金は {fmt_amount(margin)}、余力は {fmt_amount(status.free_margin)}"
                " です。数量を減らすか、レバレッジを上げてください。"
            )
        position = portfolio.open(inst.key, side, qty, price, at, leverage, take_profit_yen, stop_loss_yen)
        portfolio.save(self.path)
        lines = [
            *event_lines,
            f"新規{SIDE_LABELS[side]}: #{position.id} {inst.name} {inst.format_qty(position.qty)}"
            f" @ {inst.format_price(price)}円 ({note})"
            f"  レバレッジ {format_leverage(position.leverage)}・必要証拠金 {fmt_amount(position.required_margin)}",
        ]
        if position.take_profit is not None or position.stop_loss is not None:
            lines.append(_exit_line(inst, position))
        return Result(lines=lines)

    def set_exit(
        self, position_id: int | str, take_profit_yen: float | None = None, stop_loss_yen: float | None = None
    ) -> Result:
        """保有中の建玉に決済注文を出す (出し直す)。

        金額 (円) が None ならその注文は今のまま、0 なら取り消す。
        今の価格ですでに届いている金額は指定できない。注文を出した時点より後の値動きで判定する。
        """
        for amount in (take_profit_yen, stop_loss_yen):
            if amount is not None and amount < 0:
                raise InputError(f"利益確定・損切りの金額は 0 以上で指定してください (0 なら注文なし): {amount}")
        events = self.check_triggers()
        event_lines = events.lines if events else []
        portfolio = self.load()
        position_id = _parse_position_id(position_id)
        try:
            position = portfolio.find(position_id)
        except PortfolioError:
            if events is not None:  # 決済注文やロスカットで、ちょうど決済されたところ
                events.notes.append(f"建玉 #{position_id} はすでに決済されています。")
                return events
            raise
        inst = get_instrument(position.symbol)
        new_tp, new_sl = exit_prices(position, take_profit_yen, stop_loss_yen)
        tp = position.take_profit if take_profit_yen is None else new_tp
        sl = position.stop_loss if stop_loss_yen is None else new_sl
        if tp is not None or sl is not None:
            latest = self.source.latest(inst).price
            now = self.clock()
            reached = [
                name
                for name, probe in (
                    ("損切り", replace(position, take_profit=None, stop_loss=sl)),
                    ("利益確定", replace(position, take_profit=tp, stop_loss=None)),
                )
                if find_exit(probe, (), latest, now) is not None
            ]
            if reached:
                raise InputError(
                    f"今の価格 {inst.format_price(latest)} で、すでに{reached[0]}の価格に届いています。金額を変えてください。"
                )
        position = portfolio.set_exit(position_id, tp, sl, self.clock().strftime("%Y-%m-%d %H:%M"))
        portfolio.save(self.path)
        if tp is None and sl is None:
            return Result(lines=[*event_lines, f"決済注文を取り消しました: #{position.id} {inst.name}"])
        return Result(lines=[*event_lines, _exit_line(inst, position)])

    def close(
        self,
        ids: Sequence[int | str],
        qty: int | None = None,
        price: float | None = None,
        day: date | None = None,
    ) -> Result:
        """建玉を決済する。ids に "all" を渡すとすべての建玉。qty を指定すると一部だけ決済する。"""
        events = self.check_triggers()
        if events is not None and events.losscut:
            events.notes.append("ロスカットで、保有中の建玉はすべて決済済みです。")
            return events
        event_lines = events.lines if events else []
        portfolio = self.load()
        if [str(i).lower() for i in ids] == ["all"]:
            targets = [p.id for p in portfolio.positions]
            if not targets:
                return Result(lines=[*event_lines, "決済する建玉がありません。"])
        else:
            targets = list(dict.fromkeys(_parse_position_id(i) for i in ids))
            closed = [i for i in targets if i not in {p.id for p in portfolio.positions}]
            if closed and events is not None:  # 決済注文で、ちょうど決済されたところ
                events.notes.append(f"建玉 #{closed[0]} は決済注文ですでに決済されています。")
                return events
        if not targets:
            raise InputError("決済する建玉の ID を指定してください。")
        if len(targets) > 1 and qty is not None:
            raise InputError("一部決済 (数量の指定) は、建玉を 1 つだけ指定したときに使えます。")
        if len(targets) > 1 and price is not None:
            raise InputError("価格の指定は、建玉を 1 つだけ指定したときに使えます。")

        # 先にすべての決済価格を決めてから決済する (途中で失敗したら何も保存しない)
        orders = []
        for position_id in targets:
            inst = get_instrument(portfolio.find(position_id).symbol)
            orders.append((position_id, inst, *self._resolve_price(inst, price, day)))

        lines = []
        for position_id, inst, exit_price, at, note in orders:
            trade = portfolio.close(position_id, exit_price, at, qty)
            lines.append(_close_line(inst, trade, note))
        portfolio.save(self.path)
        lines.append(f"実現損益の通算: {fmt_yen(portfolio.realized_pnl)}")
        return Result(lines=[*event_lines, *lines])

    def positions(self) -> Result:
        events = self.check_triggers()
        event_lines = events.lines if events else []
        portfolio = self.load()
        if not portfolio.positions:
            return Result(lines=[*event_lines, "保有中の建玉はありません。"])
        prices, notes = self.latest_prices({p.symbol for p in portfolio.positions})
        rows = []
        total = 0.0
        for position in portfolio.positions:
            inst = get_instrument(position.symbol)
            price = prices.get(position.symbol)
            if price is None:
                current, pnl_text = "-", "-"
            else:
                pnl = position.unrealized_pnl(price)
                total += pnl
                current, pnl_text = inst.format_price(price), fmt_yen(pnl)
            rows.append(
                [
                    f"#{position.id}",
                    inst.name,
                    SIDE_LABELS[position.side],
                    inst.format_qty(position.qty),
                    format_leverage(position.leverage),
                    inst.format_price(position.entry_price),
                    current,
                    pnl_text,
                    fmt_amount(position.required_margin),
                    "-" if position.take_profit is None else inst.format_price(position.take_profit),
                    "-" if position.stop_loss is None else inst.format_price(position.stop_loss),
                    position.opened_at,
                ]
            )
        headers = [
            "建玉", "銘柄", "売買", "数量", "レバレッジ", "建値", "現在値", "評価損益", "必要証拠金", "利確", "損切り", "約定日時",
        ]
        suffix = " (現在値を取得できなかった建玉を除く)" if notes else ""
        used = sum(p.required_margin for p in portfolio.positions)
        return Result(
            table=Table(headers, rows, "<<<>>>>>>>><"),
            lines=[*event_lines, f"評価損益の合計: {fmt_yen(total)}{suffix}　必要証拠金の合計: {fmt_amount(used)}"],
            notes=notes,
        )

    def history(self, symbol: str | None = None) -> Result:
        trades = self.load().trades
        if symbol:
            key = find_instrument(symbol).key
            trades = [t for t in trades if t.symbol == key]
        if not trades:
            return Result(lines=["決済済みの取引はまだありません。"])
        rows = []
        for trade in trades:
            inst = get_instrument(trade.symbol)
            rows.append(
                [
                    f"#{trade.position_id}",
                    inst.name,
                    SIDE_LABELS[trade.side],
                    inst.format_qty(trade.qty),
                    format_leverage(trade.leverage),
                    inst.format_price(trade.entry_price),
                    inst.format_price(trade.exit_price),
                    fmt_yen(trade.pnl),
                    trade.opened_at,
                    trade.closed_at,
                    REASON_LABELS.get(trade.reason, ""),
                ]
            )
        headers = ["建玉", "銘柄", "売買", "数量", "レバレッジ", "建値", "決済値", "損益", "約定日時", "決済日時", "備考"]
        return Result(
            table=Table(headers, rows, "<<<>>>>><<<"),
            lines=[f"実現損益の合計: {fmt_yen(sum(t.pnl for t in trades))} ({len(trades)} 件)"],
        )

    def summary_rows(self) -> tuple[list[SummaryRow], list[str]]:
        """銘柄別の損益 (数値) と、現在値を取得できなかった銘柄のエラー。"""
        portfolio = self.load()
        prices, errors = self.latest_prices({p.symbol for p in portfolio.positions})
        return portfolio.summary(prices), errors

    def summary(self) -> Result:
        """銘柄別と通算の損益。通算損益 = 実現損益 + 評価損益。口座の状態 (証拠金) も表示する。"""
        events = self.check_triggers()
        rows, errors = self.summary_rows()
        status, _ = self.account_status()

        table_rows = [
            [
                get_instrument(r.symbol).name,
                str(r.trades),
                fmt_rate(r.win_rate),
                fmt_yen(r.realized),
                str(r.open_positions),
                fmt_yen(r.unrealized),
                fmt_yen(r.total),
            ]
            for r in rows
        ]
        trades = sum(r.trades for r in rows)
        wins = sum(r.wins for r in rows)
        realized = round(sum(r.realized for r in rows), 2)
        unrealized = round(sum(r.unrealized for r in rows if r.unrealized is not None), 2)
        missing = any(r.unrealized is None for r in rows)
        total = round(realized + unrealized, 2)
        footer = [
            [
                "合計",
                str(trades),
                fmt_rate(wins / trades if trades else None),
                fmt_yen(realized),
                str(sum(r.open_positions for r in rows)),
                "-" if missing else fmt_yen(unrealized),
                "-" if missing else fmt_yen(total),
            ]
        ]
        headers = ["銘柄", "決済回数", "勝率", "実現損益", "建玉数", "評価損益", "合計"]
        notes = []
        if missing:
            notes = ["現在値を取得できなかった銘柄の評価損益は、通算損益に含まれていません。", *errors]
        return Result(
            table=Table(headers, table_rows, "<>>>>>>", footer),
            lines=[
                *(events.lines if events else []),
                f"通算損益: {fmt_yen(total)}  (実現損益 {fmt_yen(realized)} + 評価損益 {fmt_yen(unrealized)})",
                *_account_lines(status),
            ],
            notes=notes,
        )

    # ---- 口座 ------------------------------------------------------------

    def account_status(self) -> tuple[AccountStatus, list[str]]:
        """口座の状態 (資金・有効証拠金・余力・証拠金維持率) と、現在値を取得できなかった銘柄のエラー。"""
        portfolio = self.load()
        prices, errors = self.latest_prices({p.symbol for p in portfolio.positions})
        return portfolio.status(prices), errors

    def account(self) -> Result:
        events = self.check_triggers()
        status, errors = self.account_status()
        return Result(lines=[*(events.lines if events else []), *_account_lines(status)], notes=errors)

    def set_account(self, initial_capital: float | None = None, losscut_level: float | None = None) -> Result:
        """初期資金とロスカット水準 (%、0 ならロスカットしない) を変える。"""
        portfolio = self.load()
        portfolio.set_account(initial_capital, losscut_level)
        portfolio.save(self.path)
        lines = []
        if initial_capital is not None:
            lines.append(f"初期資金を {fmt_amount(portfolio.initial_capital)} にしました。")
        if losscut_level is not None:
            lines.append(f"ロスカット水準を {fmt_losscut(portfolio.losscut_level)} にしました。")
        return Result(lines=lines)

    def check_triggers(self) -> Result | None:
        """決済注文 (利益確定・損切り) とロスカットを判定して、条件を満たした建玉を決済する。

        何も起きなければ None。ロスカットが起きたら Result.losscut が True。
        """
        exits = self.check_exit_orders()
        losscut = self.check_losscut()
        if exits is None and losscut is None:
            return None
        lines = [*(exits.lines if exits else []), *(losscut.lines if losscut else [])]
        return Result(lines=lines, losscut=losscut is not None)

    def check_exit_orders(self) -> Result | None:
        """決済注文を出した後の値動き (ローソク足) を順に見て、価格に届いた建玉を、届いた時点で決済する。

        アプリを閉じていた間の値動きも、ローソク足をさかのぼって判定する。
        """
        portfolio = self.load()
        targets = [p for p in portfolio.positions if p.take_profit is not None or p.stop_loss is not None]
        if not targets:
            return None
        now = self.clock()
        fills = []  # (時刻, 記録する日時, 建玉, 約定価格, 理由)
        get_path = getattr(self.source, "path", None)
        for inst in INSTRUMENTS:
            positions = [p for p in targets if p.symbol == inst.key]
            if not positions:
                continue
            starts = {p.id: parse_trade_time(p.exit_set_at or p.opened_at) for p in positions}
            candles: list[tuple[datetime, float, float, float]] = []
            daily = False
            if get_path is not None:
                try:
                    path = get_path(inst, min(starts.values()))
                except PriceError:
                    path = None
                if path is not None and len(path):
                    daily = path.attrs.get("interval") == "1d"
                    candles = [
                        (ts.to_pydatetime(), float(o), float(h), float(low))
                        for ts, o, h, low in zip(path.index, path["Open"], path["High"], path["Low"])
                    ]
            try:
                latest = self.source.latest(inst).price
            except PriceError:
                latest = None
            for position in positions:
                after = [c for c in candles if c[0] >= starts[position.id]]
                hit = find_exit(position, after, latest, now)
                if hit is None:
                    continue
                when, price, reason = hit
                at = when.strftime("%Y-%m-%d" if daily and when != now else "%Y-%m-%d %H:%M")
                fills.append((when, at, position, round(price, inst.price_decimals + 2), reason))
        if not fills:
            return None
        fills.sort(key=lambda fill: fill[0])
        lines = []
        for _, at, position, price, reason in fills:
            inst = get_instrument(position.symbol)
            trade = portfolio.close(position.id, price, at, reason=reason)
            lines.append(_close_line(inst, trade, f"{REASON_LABELS[reason]} {at}"))
        portfolio.save(self.path)
        lines.append(f"実現損益の通算: {fmt_yen(portfolio.realized_pnl)}")
        return Result(lines=lines)

    def check_losscut(self) -> Result | None:
        """証拠金維持率がロスカット水準を下回っていたら、すべての建玉を今の価格で決済する。

        価格を確認したとき (注文・決済・表示のたび) に判定する。アプリを閉じている間は判定しないので、
        次に開いたときの価格で決済される。
        """
        portfolio = self.load()
        if not portfolio.positions or portfolio.losscut_level <= 0:
            return None
        prices, _ = self.latest_prices({p.symbol for p in portfolio.positions})
        status = portfolio.status(prices)
        if not status.losscut_triggered:
            return None
        at = self.clock().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"ロスカット: 証拠金維持率が {fmt_level(status.margin_level)} になり、ロスカット水準 "
            f"{fmt_losscut(status.losscut_level)} を下回ったため、すべての建玉を決済しました。"
        ]
        for position in list(portfolio.positions):
            inst = get_instrument(position.symbol)
            trade = portfolio.close(position.id, prices[position.symbol], at, reason=LOSSCUT)
            lines.append(_close_line(inst, trade, "ロスカット"))
        portfolio.save(self.path)
        lines.append(f"実現損益の通算: {fmt_yen(portfolio.realized_pnl)}")
        return Result(lines=lines)

    def reset(self) -> Result:
        """建玉と取引履歴をすべて削除する。"""
        if not self.path.exists():
            return Result(lines=[f"データファイル {self.path} はありません。"])
        self.path.unlink()
        return Result(lines=["データを削除しました。"])

    # ---- 内部 ------------------------------------------------------------

    def _resolve_price(self, inst: Instrument, price: float | None, day: date | None) -> tuple[float, str, str]:
        """約定価格を決める。戻り値は (価格, 記録する約定日時, 説明)。"""
        now = self.clock()
        if day is not None and day > now.date():
            raise InputError(f"未来の日付 ({day}) は指定できません。")
        if price is not None:
            if not price > 0:
                raise InputError(f"価格は 0 より大きい値を指定してください: {price}")
            at = day.isoformat() if day else now.strftime("%Y-%m-%d %H:%M")
            return price, at, "指定価格"
        if day is not None:
            quote = self.source.on_date(inst, day)
            if quote.as_of == now.date():
                note = f"当日 {quote.as_of} の最新値"  # 取引時間中なら確定した終値ではない
            elif quote.as_of != day:
                note = f"{day} は取引がないため、直前の営業日 {quote.as_of} の終値"
            else:
                note = f"{quote.as_of} の終値"
            return quote.price, quote.as_of.isoformat(), note
        quote = self.source.latest(inst)
        return quote.price, now.strftime("%Y-%m-%d %H:%M"), f"{quote.as_of} 時点の最新値"

    def _status(self, portfolio: Portfolio) -> AccountStatus:
        prices, _ = self.latest_prices({p.symbol for p in portfolio.positions})
        return portfolio.status(prices)

    def latest_prices(self, symbols: set[str]) -> tuple[dict[str, float | None], list[str]]:
        """保有銘柄の現在値をまとめて取得する。取得できなかった銘柄は None。"""
        prices: dict[str, float | None] = {}
        errors = []
        for inst in INSTRUMENTS:
            if inst.key not in symbols:
                continue
            try:
                prices[inst.key] = self.source.latest(inst).price
            except PriceError as e:
                prices[inst.key] = None
                errors.append(str(e))
        return prices, errors


def _close_line(inst: Instrument, trade: Trade, note: str) -> str:
    return (
        f"決済: #{trade.position_id} {inst.name} {SIDE_LABELS[trade.side]} {inst.format_qty(trade.qty)}"
        f"  建値 {inst.format_price(trade.entry_price)} → 決済値 {inst.format_price(trade.exit_price)}"
        f" ({note})  損益 {fmt_yen(trade.pnl)}"
    )


def _exit_line(inst: Instrument, position: Position) -> str:
    parts = []
    if position.take_profit is not None:
        parts.append(
            f"利確 {inst.format_price(position.take_profit)} ({fmt_yen(position.exit_pnl(position.take_profit))})"
        )
    if position.stop_loss is not None:
        parts.append(f"損切り {inst.format_price(position.stop_loss)} ({fmt_yen(position.exit_pnl(position.stop_loss))})")
    return f"決済注文: #{position.id} {inst.name} {SIDE_LABELS[position.side]}　" + "・".join(parts)


def fmt_losscut(level: float) -> str:
    return "なし (ロスカットしない)" if level <= 0 else f"{level:g}%"


def _account_lines(status: AccountStatus) -> list[str]:
    return [
        f"有効証拠金: {fmt_amount(status.equity)}  (資金 {fmt_amount(status.cash)} + 評価損益 {fmt_yen(status.unrealized)})"
        f"　初期資金: {fmt_amount(status.initial_capital)}",
        f"必要証拠金: {fmt_amount(status.used_margin)}　余力: {fmt_amount(status.free_margin)}"
        f"　証拠金維持率: {fmt_level(status.margin_level)}　ロスカット水準: {fmt_losscut(status.losscut_level)}",
    ]


def _parse_position_id(value: int | str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value).strip().lstrip("#"))
    except ValueError:
        raise InputError(f"建玉 ID は数字で指定してください: {value}") from None
