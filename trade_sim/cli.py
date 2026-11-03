"""コマンドラインのエントリーポイント。 `python -m trade_sim --help` で使い方を表示する。"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import __version__
from .instruments import INSTRUMENTS, Instrument, UnknownInstrumentError, find_instrument, get_instrument
from .portfolio import LONG, SHORT, SIDE_LABELS, Portfolio, PortfolioError
from .prices import PriceError, PriceSource, YFinanceSource
from .table import render_table

DEFAULT_FILE = "portfolio.json"
FILE_ENV = "TRADE_SIM_FILE"


class CliError(Exception):
    pass


@dataclass
class Context:
    path: Path
    source: PriceSource
    now: datetime

    def load(self) -> Portfolio:
        return Portfolio.load(self.path)

    def save(self, portfolio: Portfolio) -> None:
        portfolio.save(self.path)


# ---- 表示用の整形 --------------------------------------------------------


def fmt_yen(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(value)
    return "0円" if rounded == 0 else f"{rounded:+,}円"


def fmt_rate(rate: float | None) -> str:
    return "-" if rate is None else f"{rate * 100:.0f}%"


def fmt_change(inst: Instrument, price: float, prev_close: float | None) -> str:
    if not prev_close:
        return "-"
    diff = price - prev_close
    return f"{diff:+,.{inst.price_decimals}f} ({diff / prev_close * 100:+.2f}%)"


# ---- 価格の決定 ----------------------------------------------------------


def resolve_price(
    ctx: Context, inst: Instrument, price: float | None, day: date | None
) -> tuple[float, str, str]:
    """約定価格を決める。戻り値は (価格, 記録する約定日時, 説明)。"""
    if day is not None and day > ctx.now.date():
        raise CliError(f"未来の日付 ({day}) は指定できません。")
    if price is not None:
        at = day.isoformat() if day else ctx.now.strftime("%Y-%m-%d %H:%M")
        return price, at, "指定価格"
    if day is not None:
        quote = ctx.source.on_date(inst, day)
        if quote.as_of == ctx.now.date():
            note = f"当日 {quote.as_of} の最新値"  # 取引時間中なら確定した終値ではない
        elif quote.as_of != day:
            note = f"{day} は取引がないため、直前の営業日 {quote.as_of} の終値"
        else:
            note = f"{quote.as_of} の終値"
        return quote.price, quote.as_of.isoformat(), note
    quote = ctx.source.latest(inst)
    return quote.price, ctx.now.strftime("%Y-%m-%d %H:%M"), f"{quote.as_of} 時点の最新値"


def latest_prices(ctx: Context, symbols: set[str]) -> tuple[dict[str, float | None], list[str]]:
    """保有銘柄の現在値をまとめて取得する。取得できなかった銘柄は None。"""
    prices: dict[str, float | None] = {}
    errors = []
    for inst in INSTRUMENTS:
        if inst.key not in symbols:
            continue
        try:
            prices[inst.key] = ctx.source.latest(inst).price
        except PriceError as e:
            prices[inst.key] = None
            errors.append(str(e))
    return prices, errors


# ---- コマンド ------------------------------------------------------------


def cmd_quote(args: argparse.Namespace, ctx: Context) -> int:
    instruments = [find_instrument(s) for s in args.symbols] or list(INSTRUMENTS)
    rows, errors = [], []
    for inst in instruments:
        try:
            quote = ctx.source.latest(inst)
        except PriceError as e:
            rows.append([inst.name, inst.ticker, "-", "-", "-"])
            errors.append(str(e))
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
    print(render_table(["銘柄", "ティッカー", "現在値", "前日比", "日付"], rows, "<<>><"))
    for error in errors:
        print(f"※ {error}")
    return 1 if len(errors) == len(instruments) else 0


def cmd_open(args: argparse.Namespace, ctx: Context) -> int:
    inst = find_instrument(args.symbol)
    price, at, note = resolve_price(ctx, inst, args.price, args.date)
    portfolio = ctx.load()
    position = portfolio.open(inst.key, args.side, args.qty, price, at)
    ctx.save(portfolio)
    print(
        f"新規{SIDE_LABELS[args.side]}: #{position.id} {inst.name} {inst.format_qty(position.qty)}"
        f" @ {inst.format_price(price)}円 ({note})"
    )
    return 0


def _parse_position_id(text: str) -> int:
    try:
        return int(text.lstrip("#"))
    except ValueError:
        raise CliError(f"建玉 ID は数字で指定してください: {text}") from None


def cmd_close(args: argparse.Namespace, ctx: Context) -> int:
    portfolio = ctx.load()
    if [s.lower() for s in args.ids] == ["all"]:
        ids = [p.id for p in portfolio.positions]
        if not ids:
            print("決済する建玉がありません。")
            return 0
    else:
        ids = list(dict.fromkeys(_parse_position_id(s) for s in args.ids))
    if len(ids) > 1 and args.qty is not None:
        raise CliError("--qty (一部決済) は建玉を 1 つだけ指定したときに使えます。")
    if len(ids) > 1 and args.price is not None:
        raise CliError("--price は建玉を 1 つだけ指定したときに使えます。")

    # 先にすべての決済価格を決めてから決済する (途中で失敗したら何も保存しない)
    orders = []
    for position_id in ids:
        inst = get_instrument(portfolio.find(position_id).symbol)
        orders.append((position_id, inst, *resolve_price(ctx, inst, args.price, args.date)))

    for position_id, inst, price, at, note in orders:
        trade = portfolio.close(position_id, price, at, args.qty)
        print(
            f"決済: #{trade.position_id} {inst.name} {SIDE_LABELS[trade.side]} {inst.format_qty(trade.qty)}"
            f"  建値 {inst.format_price(trade.entry_price)} → 決済値 {inst.format_price(trade.exit_price)}"
            f" ({note})  損益 {fmt_yen(trade.pnl)}"
        )
    ctx.save(portfolio)
    print(f"実現損益の通算: {fmt_yen(portfolio.realized_pnl)}")
    return 0


def cmd_positions(args: argparse.Namespace, ctx: Context) -> int:
    portfolio = ctx.load()
    if not portfolio.positions:
        print("保有中の建玉はありません。")
        return 0
    prices, errors = latest_prices(ctx, {p.symbol for p in portfolio.positions})
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
                inst.format_price(position.entry_price),
                current,
                pnl_text,
                position.opened_at,
            ]
        )
    print(render_table(["建玉", "銘柄", "売買", "数量", "建値", "現在値", "評価損益", "約定日時"], rows, "<<<>>>><"))
    print()
    suffix = " (現在値を取得できなかった建玉を除く)" if errors else ""
    print(f"評価損益の合計: {fmt_yen(total)}{suffix}")
    for error in errors:
        print(f"※ {error}")
    return 0


def cmd_history(args: argparse.Namespace, ctx: Context) -> int:
    trades = ctx.load().trades
    if args.symbol:
        key = find_instrument(args.symbol).key
        trades = [t for t in trades if t.symbol == key]
    if not trades:
        print("決済済みの取引はまだありません。")
        return 0
    rows = []
    for trade in trades:
        inst = get_instrument(trade.symbol)
        rows.append(
            [
                f"#{trade.position_id}",
                inst.name,
                SIDE_LABELS[trade.side],
                inst.format_qty(trade.qty),
                inst.format_price(trade.entry_price),
                inst.format_price(trade.exit_price),
                fmt_yen(trade.pnl),
                trade.opened_at,
                trade.closed_at,
            ]
        )
    headers = ["建玉", "銘柄", "売買", "数量", "建値", "決済値", "損益", "約定日時", "決済日時"]
    print(render_table(headers, rows, "<<<>>>><<"))
    print()
    print(f"実現損益の合計: {fmt_yen(sum(t.pnl for t in trades))} ({len(trades)} 件)")
    return 0


def cmd_summary(args: argparse.Namespace, ctx: Context) -> int:
    portfolio = ctx.load()
    prices, errors = latest_prices(ctx, {p.symbol for p in portfolio.positions})
    rows = portfolio.summary(prices)

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
    print(render_table(headers, table_rows, "<>>>>>>", footer))
    print()
    print(f"通算損益: {fmt_yen(total)}  (実現損益 {fmt_yen(realized)} + 評価損益 {fmt_yen(unrealized)})")
    if missing:
        print("※ 現在値を取得できなかった銘柄の評価損益は、通算損益に含まれていません。")
        for error in errors:
            print(f"  {error}")
    return 0


def cmd_reset(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.path.exists():
        print(f"データファイル {ctx.path} はありません。")
        return 0
    if not args.yes:
        try:
            answer = input(f"{ctx.path} の建玉と取引履歴をすべて削除します。よろしいですか? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return 0
    ctx.path.unlink()
    print("データを削除しました。")
    return 0


# ---- 引数の解析 ----------------------------------------------------------


def _positive_int(text: str) -> int:
    try:
        value = int(text.replace(",", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数で指定してください: {text}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"1 以上を指定してください: {text}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"数値で指定してください: {text}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"0 より大きい値を指定してください: {text}")
    return value


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日付は YYYY-MM-DD 形式で指定してください: {text}") from None


def _add_price_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--date",
        type=_date,
        metavar="YYYY-MM-DD",
        help="その日の終値で約定させる (休場日なら直前の営業日)。省略すると最新の価格",
    )
    parser.add_argument("--price", type=_positive_float, metavar="PRICE", help="約定価格を直接指定する")


def _epilog() -> str:
    lines = ["対応銘柄 (銘柄名・コード・ティッカーのどれでも指定できます):"]
    for inst in INSTRUMENTS:
        names = " / ".join(dict.fromkeys([inst.name, inst.key.lower(), *inst.aliases]))
        lines.append(f"  {names}  [{inst.ticker}]")
    lines += [
        "",
        "例:",
        "  python -m trade_sim quote                       現在値を表示",
        "  python -m trade_sim long kioxia 100             キオクシアを 100 株ロング",
        "  python -m trade_sim short usdjpy 10000          ドル円を 1 万通貨ショート",
        "  python -m trade_sim long 6920 100 --date 2025-04-01   過去の終値で約定",
        "  python -m trade_sim positions                   保有中の建玉と評価損益",
        "  python -m trade_sim close 1                     建玉 #1 を決済",
        "  python -m trade_sim summary                     通算損益を表示",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trade_sim",
        description="yfinance の価格で、株・為替のロング/ショート取引をシミュレーションします。",
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file",
        default=os.environ.get(FILE_ENV, DEFAULT_FILE),
        metavar="PATH",
        help=f"取引データの保存先 (既定: {DEFAULT_FILE}。環境変数 {FILE_ENV} でも指定可)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("quote", help="現在値を表示する")
    p.add_argument("symbols", nargs="*", metavar="SYMBOL", help="銘柄 (省略すると全銘柄)")
    p.set_defaults(func=cmd_quote)

    for side, text in ((LONG, "新規にロング (買い) で建てる"), (SHORT, "新規にショート (売り) で建てる")):
        p = sub.add_parser(side, help=text, description=text)
        p.add_argument("symbol", metavar="SYMBOL", help="銘柄 (例: kioxia、6920、ドル円、eurjpy)")
        p.add_argument("qty", type=_positive_int, metavar="QTY", help="数量。株数または通貨数 (例: 100、10000)")
        _add_price_options(p)
        p.set_defaults(func=cmd_open, side=side)

    p = sub.add_parser("close", help="建玉を決済する", description="建玉を決済する")
    p.add_argument("ids", nargs="+", metavar="ID", help="決済する建玉の ID (複数可)。all ですべての建玉")
    p.add_argument("--qty", type=_positive_int, metavar="QTY", help="一部だけ決済する数量")
    _add_price_options(p)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("positions", help="保有中の建玉と評価損益を表示する")
    p.set_defaults(func=cmd_positions)

    p = sub.add_parser("history", help="決済済みの取引履歴を表示する")
    p.add_argument("symbol", nargs="?", metavar="SYMBOL", help="銘柄 (指定するとその銘柄だけ表示)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("summary", aliases=["pnl"], help="銘柄別と通算の損益を表示する")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("reset", help="建玉と取引履歴をすべて削除する")
    p.add_argument("-y", "--yes", action="store_true", help="確認せずに削除する")
    p.set_defaults(func=cmd_reset)

    return parser


def main(
    argv: list[str] | None = None,
    source: PriceSource | None = None,
    now: datetime | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    ctx = Context(Path(args.file), source or YFinanceSource(), now or datetime.now())
    try:
        return args.func(args, ctx)
    except (CliError, PortfolioError, PriceError, UnknownInstrumentError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
