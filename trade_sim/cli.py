"""コマンドラインのエントリーポイント。 `python -m trade_sim --help` で使い方を表示する。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from . import __version__
from .instruments import INSTRUMENTS
from .portfolio import LONG, SHORT
from .prices import PriceSource
from .simulator import USER_ERRORS, Result, Simulator, now_jst, parse_date
from .table import render_table

DEFAULT_FILE = "portfolio.json"
FILE_ENV = "TRADE_SIM_FILE"


def print_result(result: Result) -> None:
    if result.table is not None:
        table = result.table
        print(render_table(table.headers, table.rows, table.aligns, table.footer))
        if result.lines or result.notes:
            print()
    for line in result.lines:
        print(line)
    for note in result.notes:
        print(f"※ {note}")


# ---- コマンド ------------------------------------------------------------


def cmd_quote(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.quote(args.symbols)


def cmd_open(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.open(
        args.symbol,
        args.side,
        args.qty,
        price=args.price,
        day=args.date,
        leverage=args.leverage,
        take_profit_yen=args.tp,
        stop_loss_yen=args.sl,
    )


def cmd_tpsl(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.set_exit(args.id, take_profit_yen=args.tp, stop_loss_yen=args.sl)


def cmd_close(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.close(args.ids, qty=args.qty, price=args.price, day=args.date)


def cmd_positions(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.positions()


def cmd_history(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.history(args.symbol)


def cmd_summary(args: argparse.Namespace, sim: Simulator) -> Result:
    return sim.summary()


def cmd_account(args: argparse.Namespace, sim: Simulator) -> Result:
    if args.capital is None and args.losscut is None:
        return sim.account()
    changed = sim.set_account(initial_capital=args.capital, losscut_level=args.losscut)
    status = sim.account()
    return Result(lines=[*changed.lines, *status.lines], notes=status.notes)


def cmd_reset(args: argparse.Namespace, sim: Simulator) -> Result:
    if sim.path.exists() and not args.yes:
        try:
            answer = input(f"{sim.path} の建玉と取引履歴をすべて削除します。よろしいですか? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return Result(lines=["中止しました。"])
    return sim.reset()


# ---- 引数の解析 ----------------------------------------------------------


def _positive_int(text: str) -> int:
    try:
        value = int(text.replace(",", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数で指定してください: {text}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"1 以上を指定してください: {text}")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text.replace(",", "").rstrip("%"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"数値で指定してください: {text}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"0 以上を指定してください: {text}")
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
        day = parse_date(text)
    except USER_ERRORS as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    if day is None:
        raise argparse.ArgumentTypeError("日付を YYYY-MM-DD 形式で指定してください")
    return day


def _add_exit_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tp", type=_non_negative_float, metavar="YEN", help="この金額の利益になったら決済する (利益確定。円)"
    )
    parser.add_argument(
        "--sl", type=_non_negative_float, metavar="YEN", help="この金額の損失になったら決済する (損切り。円)"
    )


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
        "  python -m trade_sim short usdjpy 10000 -l 10    ドル円を 1 万通貨、レバレッジ 10 倍でショート",
        "  python -m trade_sim long usdjpy 10000 --tp 5000 --sl 3000   +5,000円で利確・-3,000円で損切りする注文つき",
        "  python -m trade_sim long 6920 100 --date 2025-04-01   過去の終値で約定",
        "  python -m trade_sim positions                   保有中の建玉と評価損益",
        "  python -m trade_sim close 1                     建玉 #1 を決済",
        "  python -m trade_sim tpsl 1 --tp 20000 --sl 0    建玉 #1 の利確を +20,000円 に変え、損切りを取り消す",
        "  python -m trade_sim summary                     通算損益を表示",
        "  python -m trade_sim account                     資金・証拠金・余力・証拠金維持率を表示",
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
        p.add_argument(
            "-l",
            "--leverage",
            type=_positive_float,
            default=1.0,
            metavar="X",
            help="レバレッジ (株は 3.3 倍、為替は 500 倍まで。既定: 1 倍)",
        )
        _add_exit_options(p)
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

    p = sub.add_parser(
        "tpsl",
        help="保有中の建玉に決済注文 (利益確定・損切り) を出す・変える",
        description=(
            "保有中の建玉に、指定した金額の利益・損失になったら決済する注文を出します。"
            "指定しなかった方は今の注文のまま、0 を指定するとその注文を取り消します。"
        ),
    )
    p.add_argument("id", metavar="ID", help="建玉の ID")
    _add_exit_options(p)
    p.set_defaults(func=cmd_tpsl)

    p = sub.add_parser(
        "account",
        help="資金・証拠金・余力・証拠金維持率を表示する (初期資金やロスカット水準の変更も)",
        description="口座の状態を表示します。オプションを付けると設定を変えます。",
    )
    p.add_argument("--capital", type=_positive_float, metavar="YEN", help="初期資金を変える (例: 10000000)")
    p.add_argument(
        "--losscut",
        type=_non_negative_float,
        metavar="PERCENT",
        help="ロスカット水準 (証拠金維持率 %%) を変える。0 ならロスカットしない (既定: 50)",
    )
    p.set_defaults(func=cmd_account)

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
    clock = (lambda: now) if now is not None else now_jst
    sim = Simulator(args.file, source, clock)
    try:
        result = args.func(args, sim)
    except USER_ERRORS as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print_result(result)
    return 0 if result.ok else 1
