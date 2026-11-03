"""ブラウザで使う取引シミュレーター (Streamlit)。

起動:  streamlit run app.py
"""

from __future__ import annotations

import html
import math
import os
from collections.abc import Callable
from datetime import date

import pandas as pd
import streamlit as st

from .charts import DOWN, UP, candle_figure, equity_figure, pnl_by_instrument_figure
from .instruments import INSTRUMENTS, LEVERAGE_CHOICES, Instrument, format_leverage, get_instrument
from .portfolio import LONG, LOSSCUT, SHORT, SIDE_LABELS, AccountStatus, Portfolio, SummaryRow
from .prices import TIMEFRAMES, PriceError, YFinanceSource, get_timeframe
from .simulator import (
    USER_ERRORS,
    Result,
    Simulator,
    fmt_amount,
    fmt_change,
    fmt_level,
    fmt_losscut,
    fmt_rate,
    fmt_yen,
)

DEFAULT_FILE = os.environ.get("TRADE_SIM_FILE", "portfolio.json")
DEFAULT_QTY = {"stock": 100, "fx": 10000}
QTY_STEP = {"stock": 100, "fx": 1000}
REFRESH_CHOICES = {"30秒": 30, "1分": 60, "5分": 300}
SIDE_CHOICES = {LONG: "ロング（買い）", SHORT: "ショート（売り）"}
OLDEST_DATE = date(2000, 1, 1)
WARN = "#d97706"  # 証拠金維持率が低いときの色


# ---- 状態 ----------------------------------------------------------------


@st.cache_resource
def _shared_source() -> YFinanceSource:
    """価格の取得元。取得済みの価格を使い回すため、アプリ全体で 1 つにする。"""
    return YFinanceSource()


def _simulator() -> Simulator:
    # テストでは session_state["price_source"] にテスト用の価格ソースを入れる
    source = st.session_state.get("price_source") or _shared_source()
    return Simulator(st.session_state.get("data_file") or DEFAULT_FILE, source)


def _name(key: str) -> str:
    return get_instrument(key).name


# ---- ボタンの処理 (コールバック) ------------------------------------------
# コールバックは画面を描き直す前に実行されるので、画面全体に操作の結果が反映される。


def _run_action(action: Callable[[], Result]) -> None:
    st.session_state.pop("losscut_notice", None)  # 前のロスカットの知らせは、次の操作で消す
    try:
        result = action()
    except USER_ERRORS as e:
        st.session_state["flash"] = {"kind": "error", "text": str(e), "toast": False}
        return
    text = "\n".join([*result.lines, *(f"※ {note}" for note in result.notes)])
    st.session_state["flash"] = {"kind": "success" if result.ok else "error", "text": text, "toast": result.ok}


def _place_order() -> None:
    state = st.session_state
    inst = get_instrument(state["symbol"])
    sim = _simulator()
    _run_action(
        lambda: sim.open(
            inst.key,
            state["order_side"],
            int(state[f"order_qty_{inst.kind}"]),
            day=state.get("order_date"),
            leverage=float(state[f"order_leverage_{inst.kind}"]),
        )
    )


def _close_selected() -> None:
    state = st.session_state
    sim = _simulator()
    qty = int(state.get("close_qty") or 0) or None  # 0 ならすべて決済
    _run_action(lambda: sim.close([state["close_id"]], qty=qty, day=state.get("close_date")))
    state["close_qty"] = 0


def _close_all() -> None:
    state = st.session_state
    sim = _simulator()
    _run_action(lambda: sim.close(["all"], day=state.get("close_date")))


def _save_account() -> None:
    state = st.session_state
    sim = _simulator()
    _run_action(
        lambda: sim.set_account(
            initial_capital=float(state["account_capital"]), losscut_level=float(state["account_losscut"])
        )
    )


def _refresh_prices() -> None:
    # 前の操作のメッセージは消す
    st.session_state.pop("flash", None)
    st.session_state.pop("losscut_notice", None)
    _simulator().refresh_prices()


# ---- 表 ------------------------------------------------------------------


def _pnl_color(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if value > 0:
        return f"color: {UP}"
    if value < 0:
        return f"color: {DOWN}"
    return ""


def _show_table(df: pd.DataFrame, pnl_columns: tuple[str, ...] = (), right_columns: tuple[str, ...] = ()) -> None:
    """損益の列は赤 (プラス) / 青 (マイナス) で表示し、数字の列は右寄せにする。"""
    styler = df.style.format({c: fmt_yen for c in pnl_columns}, na_rep="-")
    if pnl_columns:
        styler = styler.map(_pnl_color, subset=list(pnl_columns))
    config = {c: st.column_config.TextColumn(alignment="right") for c in right_columns}
    config.update({c: st.column_config.NumberColumn(alignment="right") for c in pnl_columns})
    st.dataframe(styler, hide_index=True, column_config=config)


def _positions_frame(portfolio: Portfolio, prices: dict[str, float | None]) -> pd.DataFrame:
    rows = []
    for p in portfolio.positions:
        inst = get_instrument(p.symbol)
        price = prices.get(p.symbol)
        rows.append(
            {
                "建玉": f"#{p.id}",
                "銘柄": inst.name,
                "売買": SIDE_LABELS[p.side],
                "数量": inst.format_qty(p.qty),
                "レバレッジ": format_leverage(p.leverage),
                "建値": inst.format_price(p.entry_price),
                "現在値": inst.format_price(price) if price is not None else "-",
                "評価損益": p.unrealized_pnl(price) if price is not None else None,
                "必要証拠金": fmt_amount(p.required_margin),
                "約定日時": p.opened_at,
            }
        )
    return pd.DataFrame(rows)


def _summary_frame(rows: list[SummaryRow]) -> pd.DataFrame:
    records = [
        {
            "銘柄": get_instrument(r.symbol).name,
            "決済回数": r.trades,
            "勝率": fmt_rate(r.win_rate),
            "実現損益": r.realized,
            "建玉数": r.open_positions,
            "評価損益": r.unrealized,
            "合計": r.total,
        }
        for r in rows
    ]
    trades = sum(r.trades for r in rows)
    missing = any(r.unrealized is None for r in rows)
    unrealized = sum(r.unrealized for r in rows if r.unrealized is not None)
    realized = sum(r.realized for r in rows)
    records.append(
        {
            "銘柄": "合計",
            "決済回数": trades,
            "勝率": fmt_rate(sum(r.wins for r in rows) / trades if trades else None),
            "実現損益": realized,
            "建玉数": sum(r.open_positions for r in rows),
            "評価損益": None if missing else unrealized,
            "合計": None if missing else realized + unrealized,
        }
    )
    return pd.DataFrame(records)


def _history_frame(portfolio: Portfolio) -> pd.DataFrame:
    rows = []
    for t in reversed(portfolio.trades):  # 新しい順
        inst = get_instrument(t.symbol)
        rows.append(
            {
                "建玉": f"#{t.position_id}",
                "銘柄": inst.name,
                "売買": SIDE_LABELS[t.side],
                "数量": inst.format_qty(t.qty),
                "レバレッジ": format_leverage(t.leverage),
                "建値": inst.format_price(t.entry_price),
                "決済値": inst.format_price(t.exit_price),
                "損益": t.pnl,
                "約定日時": t.opened_at,
                "決済日時": t.closed_at,
                "備考": "ロスカット" if t.reason == LOSSCUT else "",
            }
        )
    return pd.DataFrame(rows)


# ---- 画面の部品 ------------------------------------------------------------


def _kpi(
    column: st.delta_generator.DeltaGenerator, label: str, text: str, note: str, help: str, color: str | None = None
) -> None:
    """大きな数字のカード。どれも同じ構成にして高さをそろえる。"""
    style = "font-size: clamp(1.15rem, 2vw, 1.75rem); font-weight: 700; line-height: 1.3; white-space: nowrap;"
    if color:
        style += f" color: {color};"
    with column.container(border=True):
        st.caption(label, help=help)
        st.markdown(f'<div style="{style}">{html.escape(text)}</div>', unsafe_allow_html=True)
        st.caption(note)


def _pnl_color_of(value: float | None) -> str | None:
    if value is None or round(value) == 0:
        return None
    return UP if value > 0 else DOWN


def _account_header(status: AccountStatus, errors: list[str]) -> None:
    total = status.realized + (status.unrealized or 0)
    level = status.margin_level
    level_color = None
    if level is not None and status.losscut_level > 0 and level < status.losscut_level * 2:
        level_color = WARN  # ロスカット水準の 2 倍を下回ったら注意の色にする

    columns = st.columns(4)
    _kpi(
        columns[0], "有効証拠金", fmt_amount(status.equity), f"初期資金 {fmt_amount(status.initial_capital)}",
        "資金（初期資金 + 実現損益）に評価損益を足した金額",
    )
    _kpi(
        columns[1], "通算損益", fmt_yen(total), f"実現 {fmt_yen(status.realized)}・評価 {fmt_yen(status.unrealized)}",
        "決済済みの損益（実現損益）と、保有中の建玉の含み損益（評価損益）の合計", _pnl_color_of(total),
    )
    _kpi(
        columns[2], "余力", fmt_amount(status.free_margin), f"必要証拠金 {fmt_amount(status.used_margin)}",
        "新しく建てるのに使える証拠金（有効証拠金 − 必要証拠金）",
    )
    _kpi(
        columns[3], "証拠金維持率", fmt_level(level), f"ロスカット水準 {fmt_losscut(status.losscut_level)}",
        "有効証拠金 ÷ 必要証拠金。ロスカット水準を下回ると、すべての建玉を決済します", level_color,
    )
    if errors:
        st.warning("現在値を取得できなかった銘柄があります。評価損益・余力が正しくない可能性があります。  \n" + "  \n".join(errors))


def _quotes(sim: Simulator) -> None:
    columns = st.columns(len(INSTRUMENTS))
    for column, inst in zip(columns, INSTRUMENTS):
        try:
            quote = sim.source.latest(inst)
        except PriceError as e:
            column.metric(inst.name, "-", help=str(e), border=True)
            continue
        diff = quote.price - quote.prev_close if quote.prev_close else 0
        column.metric(
            inst.name,
            inst.format_price(quote.price),
            delta=fmt_change(inst, quote.price, quote.prev_close) if quote.prev_close else None,
            delta_color="red" if diff > 0 else "blue" if diff < 0 else "off",
            help=f"{inst.ticker}　{quote.as_of} 時点",
            border=True,
        )


def _show_flash() -> None:
    flash = st.session_state.get("flash")
    if not flash:
        return
    text = flash["text"].replace("\n", "  \n")
    if flash["kind"] == "error":
        st.error(text, icon=":material/error:")
    else:
        st.success(text, icon=":material/check_circle:")
        if flash["toast"]:
            st.toast(flash["text"].splitlines()[0], icon=":material/check_circle:")
            flash["toast"] = False


def _estimate_html(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div style="opacity: 0.75;">{html.escape(label)}</div>'
        f'<div style="text-align: right; font-variant-numeric: tabular-nums;">{html.escape(value)}</div>'
        for label, value in rows
    )
    return f'<div style="display: grid; grid-template-columns: auto auto; gap: 2px 12px; font-size: 0.9rem;">{cells}</div>'


# ---- 取引タブ --------------------------------------------------------------


def _order_panel(sim: Simulator, inst: Instrument, status: AccountStatus, today: date) -> None:
    """チャートの横に置く注文パネル。チャートで選んでいる銘柄を注文する。"""
    state = st.session_state
    kind = inst.kind
    qty_key, leverage_key = f"order_qty_{kind}", f"order_leverage_{kind}"
    state.setdefault(qty_key, DEFAULT_QTY[kind])
    state.setdefault(leverage_key, 1.0)
    state.setdefault("order_side", LONG)

    with st.container(border=True):
        st.markdown(f"**{inst.name}を注文**")
        try:
            quote = sim.source.latest(inst)
        except PriceError as e:
            quote = None
            st.warning(str(e))
        if quote is not None:
            diff = quote.price - quote.prev_close if quote.prev_close else 0
            st.metric(
                "現在値",
                f"{inst.format_price(quote.price)}円",
                delta=fmt_change(inst, quote.price, quote.prev_close) if quote.prev_close else None,
                delta_color="red" if diff > 0 else "blue" if diff < 0 else "off",
                help=f"{quote.as_of} 時点",
            )
        st.segmented_control(
            "売買", list(SIDE_CHOICES), format_func=SIDE_CHOICES.get, required=True, key="order_side", width="stretch"
        )
        st.number_input(f"数量（{inst.unit}）", min_value=1, step=QTY_STEP[kind], key=qty_key)
        st.select_slider(
            f"レバレッジ（{format_leverage(inst.max_leverage)}まで）",
            options=list(LEVERAGE_CHOICES[kind]),
            format_func=format_leverage,
            key=leverage_key,
        )
        st.date_input(
            "約定日（空欄なら最新の価格）",
            value=None,
            min_value=OLDEST_DATE,
            max_value=today,
            key="order_date",
            help="日付を入れると、その日の終値で約定します（休場日なら直前の営業日）。",
        )

        if quote is not None:
            qty, leverage = int(state[qty_key]), float(state[leverage_key])
            notional = quote.price * qty
            margin = notional / leverage
            free = status.free_margin
            rows = [("取引金額", fmt_amount(notional)), ("必要証拠金", fmt_amount(margin)), ("余力", fmt_amount(free))]
            if free is not None:
                step = QTY_STEP[kind]
                max_qty = max(0, math.floor(free * leverage / quote.price / step) * step)
                rows.append(("建てられる最大数量", inst.format_qty(max_qty)))
            st.markdown(_estimate_html(rows), unsafe_allow_html=True)
            if free is not None and margin > free:
                st.warning("余力が足りません。数量を減らすか、レバレッジを上げてください。")
            if state.get("order_date"):
                st.caption("約定日を入れた場合、取引金額はその日の終値で計算し直します。")

        label = "ロング（買い）で発注" if state["order_side"] == LONG else "ショート（売り）で発注"
        st.button(label, type="primary", on_click=_place_order, disabled=quote is None, width="stretch")


def _positions_section(sim: Simulator, portfolio: Portfolio, today: date) -> None:
    st.subheader("保有中の建玉")
    if not portfolio.positions:
        st.info("保有中の建玉はありません。チャート右の注文パネルから建てられます。")
        return
    prices, _ = sim.latest_prices({p.symbol for p in portfolio.positions})
    _show_table(
        _positions_frame(portfolio, prices),
        pnl_columns=("評価損益",),
        right_columns=("数量", "レバレッジ", "建値", "現在値", "必要証拠金"),
    )
    labels = {}
    for p in portfolio.positions:
        inst = get_instrument(p.symbol)
        labels[p.id] = (
            f"#{p.id} {inst.name} {SIDE_LABELS[p.side]} {inst.format_qty(p.qty)}"
            f" @ {inst.format_price(p.entry_price)}（{format_leverage(p.leverage)}）"
        )
    c1, c2, c3 = st.columns([4, 2, 2], vertical_alignment="bottom")
    c1.selectbox("決済する建玉", list(labels), format_func=labels.get, key="close_id")
    c2.number_input("数量（0 ならすべて）", min_value=0, step=1, key="close_qty")
    c3.date_input("決済日（空欄なら最新の価格）", value=None, min_value=OLDEST_DATE, max_value=today, key="close_date")
    with st.container(horizontal=True):
        st.button("決済する", type="primary", on_click=_close_selected, icon=":material/check:")
        with st.popover("すべて決済", icon=":material/done_all:"):
            st.write(f"保有中の建玉 {len(portfolio.positions)} 件をすべて決済します。")
            st.button("すべて決済する", type="primary", on_click=_close_all, key="close_all_confirm")


def _trade_tab(sim: Simulator, portfolio: Portfolio, status: AccountStatus) -> None:
    _show_flash()
    today = sim.clock().date()
    c1, c2, c3, c4 = st.columns([2, 5, 2, 2], vertical_alignment="bottom")
    symbol = c1.selectbox("銘柄", [i.key for i in INSTRUMENTS], format_func=_name, key="symbol")
    timeframe_key = c2.segmented_control(
        "足",
        [t.key for t in TIMEFRAMES],
        format_func=lambda key: get_timeframe(key).label,
        default="1d",
        key="chart_timeframe",
    )
    timeframe = get_timeframe(timeframe_key or "1d")
    periods = {period: label for label, period in timeframe.periods}
    period = c3.selectbox(
        "期間",
        list(periods),
        format_func=periods.get,
        index=list(periods).index(timeframe.default_period),
        key=f"chart_period_{timeframe.key}",
    )
    show_ma = c4.checkbox("移動平均線", value=True, key="chart_ma")
    inst = get_instrument(symbol)

    chart_column, order_column = st.columns([7, 3], gap="medium")
    with chart_column:
        try:
            candles = sim.source.candles(inst, timeframe, period)
        except PriceError as e:
            st.warning(str(e))
        else:
            figure = candle_figure(inst, candles, timeframe, period, portfolio.trades, portfolio.positions, show_ma=show_ma)
            st.plotly_chart(figure, key="candle_chart", config={"displaylogo": False, "scrollZoom": True})
            st.caption(
                "▲ ロングで建てた位置　▼ ショートで建てた位置　× 決済した位置　点線は保有中の建値。"
                " ドラッグで移動、ホイールで拡大できます。"
            )
    with order_column:
        _order_panel(sim, inst, status, today)

    _positions_section(sim, portfolio, today)


# ---- 損益・履歴タブ ---------------------------------------------------------


def _pnl_tab(portfolio: Portfolio, rows: list[SummaryRow], errors: list[str], sim: Simulator) -> None:
    if not portfolio.trades and not portfolio.positions:
        st.info("まだ取引がありません。「取引」タブで取引すると、ここに損益の推移が表示されます。")
        return
    trades = len(portfolio.trades)
    wins = sum(1 for t in portfolio.trades if t.pnl > 0)
    losses = sum(1 for t in portfolio.trades if t.pnl < 0)
    realized = sum(r.realized for r in rows)
    c1, c2, c3 = st.columns(3)
    _kpi(c1, "実現損益", fmt_yen(realized), f"決済 {trades} 回", "決済した取引の損益の合計", _pnl_color_of(realized))
    _kpi(c2, "勝率", fmt_rate(wins / trades if trades else None), f"{wins}勝 {losses}敗", "利益が出た決済の割合")
    losscuts = sum(1 for t in portfolio.trades if t.reason == LOSSCUT)
    _kpi(c3, "ロスカット", f"{losscuts} 回", "強制決済された建玉の数", "ロスカットで決済された建玉の数")

    current_total = None
    if not errors:
        current_total = sum(r.realized for r in rows) + sum(r.unrealized or 0 for r in rows)
    left, right = st.columns(2)
    with left:
        st.subheader("通算損益の推移")
        st.plotly_chart(
            equity_figure(portfolio.trades, current_total, sim.clock()), key="equity_chart", config={"displaylogo": False}
        )
    with right:
        st.subheader("銘柄別の損益")
        st.plotly_chart(pnl_by_instrument_figure(rows), key="pnl_chart", config={"displaylogo": False})
    st.subheader("銘柄別の内訳")
    _show_table(_summary_frame(rows), pnl_columns=("実現損益", "評価損益", "合計"), right_columns=("決済回数", "勝率", "建玉数"))


def _history_tab(portfolio: Portfolio) -> None:
    if not portfolio.trades:
        st.info("決済済みの取引はまだありません。")
        return
    frame = _history_frame(portfolio)
    _show_table(frame, pnl_columns=("損益",), right_columns=("数量", "レバレッジ", "建値", "決済値"))
    st.download_button(
        "CSV で保存",
        frame.to_csv(index=False).encode("utf-8-sig"),  # Excel で文字化けしないよう BOM 付き
        file_name="trade_history.csv",
        mime="text/csv",
        icon=":material/download:",
    )


# ---- 全体 ----------------------------------------------------------------


def _dashboard() -> None:
    sim = _simulator()
    try:
        losscut = sim.check_losscut()
        portfolio = sim.load()
        status, errors = sim.account_status()
        rows, _ = sim.summary_rows()
    except USER_ERRORS as e:
        st.error(str(e))
        return
    if losscut is not None:
        st.session_state["losscut_notice"] = "\n".join(losscut.lines)
    notice = st.session_state.get("losscut_notice")
    if notice:
        st.error(notice.replace("\n", "  \n"), icon=":material/warning:")

    st.caption(f"最終更新 {sim.clock():%Y-%m-%d %H:%M:%S}（日本時間）　データ: {sim.path}")
    _account_header(status, errors)
    _quotes(sim)
    trade, pnl, history = st.tabs(["取引", "損益", "取引履歴"], key="main_tabs")
    with trade:
        _trade_tab(sim, portfolio, status)
    with pnl:
        _pnl_tab(portfolio, rows, errors, sim)
    with history:
        _history_tab(portfolio)


def _sidebar() -> None:
    state = st.session_state
    state.setdefault("data_file", DEFAULT_FILE)
    state.setdefault("refresh_interval", "1分")
    with st.sidebar:
        st.header("設定")
        st.text_input("取引データの保存先", key="data_file", help="同じファイルを使えば、コマンドライン版とデータを共有できます。")
        st.toggle("価格を自動で更新する", key="auto_refresh")
        st.selectbox("更新の間隔", list(REFRESH_CHOICES), key="refresh_interval", disabled=not state.get("auto_refresh"))
        st.button("価格を今すぐ更新", on_click=_refresh_prices, icon=":material/refresh:", width="stretch")

        st.subheader("口座")
        try:
            portfolio = _simulator().load()
        except USER_ERRORS as e:
            st.error(str(e))
        else:
            # データファイルを切り替えたら、そのファイルの設定を読み込み直す
            if state.get("account_loaded_from") != state["data_file"]:
                state["account_capital"] = int(portfolio.initial_capital)
                state["account_losscut"] = float(portfolio.losscut_level)
                state["account_loaded_from"] = state["data_file"]
            st.number_input(
                "初期資金（円）", min_value=100_000, step=1_000_000, key="account_capital", on_change=_save_account
            )
            st.number_input(
                "ロスカット水準（証拠金維持率 %）",
                min_value=0.0,
                max_value=100.0,
                step=10.0,
                key="account_losscut",
                on_change=_save_account,
                help="証拠金維持率がこれを下回ると、すべての建玉を決済します。0 にするとロスカットしません。",
            )
        st.divider()
        st.caption(
            "価格は Yahoo Finance のデータです（東証の銘柄はおよそ 20 分遅れ）。"
            "手数料、スプレッド、スワップポイント、信用取引の金利・貸株料は含みません。"
            "ロスカットは、アプリで価格を確認したとき（表示・注文・自動更新）に判定します。"
        )


def main() -> None:
    st.set_page_config(
        page_title="取引シミュレーター",
        page_icon=":material/candlestick_chart:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _sidebar()
    st.title("株・為替 取引シミュレーター")
    interval = REFRESH_CHOICES[st.session_state["refresh_interval"]] if st.session_state.get("auto_refresh") else None
    st.fragment(_dashboard, run_every=interval)()
