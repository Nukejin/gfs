"""チャート (Plotly) を作る。

色は日本の証券会社の表示に合わせて、値上がり・プラスを赤、値下がり・マイナスを青にしている。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .instruments import Instrument, get_instrument
from .portfolio import LONG, SIDE_LABELS, Position, SummaryRow, Trade
from .prices import Timeframe
from .simulator import fmt_yen

UP = "#d9434f"  # 陽線・プラス
DOWN = "#2f6bd6"  # 陰線・マイナス
EXIT = "#8a8f98"  # 決済の印
MA_COLORS = ("#e8890c", "#2a9d63", "#9549c4")
GRID = "rgba(127, 127, 127, 0.18)"


def trade_time(text: str) -> datetime:
    """記録された約定日時を datetime にする。日付だけのもの (終値で約定) はその日の取引終了後として扱う。"""
    if len(text) <= 10:
        return datetime.fromisoformat(text) + timedelta(hours=23, minutes=59)
    return datetime.fromisoformat(text)


def locate(index: pd.DatetimeIndex, when: datetime) -> int | None:
    """when を含むローソク足の位置。チャートの最初の足より前なら None。"""
    position = int(index.searchsorted(pd.Timestamp(when), side="right")) - 1
    return position if position >= 0 else None


_PERIOD_OFFSETS = {
    "1mo": pd.DateOffset(months=1),
    "3mo": pd.DateOffset(months=3),
    "6mo": pd.DateOffset(months=6),
    "1y": pd.DateOffset(years=1),
    "2y": pd.DateOffset(years=2),
    "5y": pd.DateOffset(years=5),
    "10y": pd.DateOffset(years=10),
}


def visible_start(index: pd.DatetimeIndex, period: str) -> int:
    """表示する期間 (period) の最初の足の位置。それより前の足は移動平均線の計算にだけ使う。"""
    if len(index) == 0 or period == "max":
        return 0
    if period in ("1d", "5d"):
        days = sorted(set(index.normalize()))
        count = 1 if period == "1d" else 5
        cutoff = days[-count] if len(days) >= count else days[0]
    else:
        cutoff = index[-1] - _PERIOD_OFFSETS[period]
    return int(index.searchsorted(cutoff, side="left"))


def _labels(index: pd.DatetimeIndex, timeframe: Timeframe) -> list[str]:
    # 夜間や休日のすき間ができないように、横軸は時刻ではなく足の並び (カテゴリ) にする
    fmt = "%m/%d %H:%M" if timeframe.intraday else "%Y/%m/%d"
    return [ts.strftime(fmt) for ts in index]


def _ma_name(window: int, timeframe: Timeframe) -> str:
    if timeframe.key == "1d":
        return f"{window}日線"
    if timeframe.key == "1wk":
        return f"{window}週線"
    return f"移動平均({window})"


def candle_figure(
    inst: Instrument,
    candles: pd.DataFrame,
    timeframe: Timeframe,
    period: str,
    trades: Sequence[Trade] = (),
    positions: Sequence[Position] = (),
    show_ma: bool = True,
    show_volume: bool = True,
) -> go.Figure:
    """ローソク足チャート。移動平均線、出来高、自分の売買の位置も描く。

    candles には表示する期間より前の足も含めてよい (移動平均線の計算にだけ使う)。
    """
    start = visible_start(candles.index, period)
    averages = {window: candles["Close"].rolling(window).mean().iloc[start:] for window in timeframe.ma_windows}
    candles = candles.iloc[start:]
    labels = _labels(candles.index, timeframe)
    has_volume = show_volume and inst.kind == "stock" and float(candles["Volume"].sum()) > 0
    if has_volume:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)
    else:
        fig = make_subplots(rows=1, cols=1)

    fig.add_trace(
        go.Candlestick(
            x=labels,
            open=candles["Open"],
            high=candles["High"],
            low=candles["Low"],
            close=candles["Close"],
            name=inst.name,
            increasing={"line": {"color": UP, "width": 1}, "fillcolor": UP},
            decreasing={"line": {"color": DOWN, "width": 1}, "fillcolor": DOWN},
        ),
        row=1,
        col=1,
    )

    if show_ma:
        for window, color in zip(timeframe.ma_windows, MA_COLORS):
            average = averages[window]
            if average.notna().any():
                fig.add_trace(
                    go.Scatter(
                        x=labels,
                        y=average,
                        mode="lines",
                        name=_ma_name(window, timeframe),
                        line={"color": color, "width": 1.4},
                        hovertemplate=f"%{{y:,.{inst.price_decimals}f}}",
                    ),
                    row=1,
                    col=1,
                )

    if has_volume:
        colors = [UP if c >= o else DOWN for o, c in zip(candles["Open"], candles["Close"])]
        fig.add_trace(
            go.Bar(x=labels, y=candles["Volume"], name="出来高", marker={"color": colors, "opacity": 0.55}),
            row=2,
            col=1,
        )

    _add_trade_marks(fig, inst, candles, labels, trades, positions)

    price_format = f",.{inst.price_decimals}f"
    fig.update_layout(
        height=600 if has_volume else 520,
        margin={"l": 8, "r": 8, "t": 36, "b": 8},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
        xaxis_rangeslider_visible=False,
        dragmode="pan",
    )
    fig.update_xaxes(type="category", nticks=8, showgrid=True, gridcolor=GRID, showspikes=True, spikethickness=1)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, tickformat=price_format, row=1, col=1)
    if has_volume:
        fig.update_yaxes(showgrid=True, gridcolor=GRID, tickformat="~s", row=2, col=1)
    return fig


def _add_trade_marks(
    fig: go.Figure,
    inst: Instrument,
    candles: pd.DataFrame,
    labels: list[str],
    trades: Sequence[Trade],
    positions: Sequence[Position],
) -> None:
    """建てた位置 (▲ロング / ▼ショート) と決済した位置 (×) を描き、保有中の建値に線を引く。"""
    index = candles.index
    entries: dict[int, tuple[str, str, float, int]] = {}  # 建玉 ID -> (side, opened_at, 建値, 数量)
    exits_x, exits_y, exits_text = [], [], []
    for trade in trades:
        if trade.symbol != inst.key:
            continue
        entries.setdefault(trade.position_id, (trade.side, trade.opened_at, trade.entry_price, 0))
        side, opened_at, entry, qty = entries[trade.position_id]
        entries[trade.position_id] = (side, opened_at, entry, qty + trade.qty)
        i = locate(index, trade_time(trade.closed_at))
        if i is not None:
            exits_x.append(labels[i])
            exits_y.append(trade.exit_price)
            exits_text.append(
                f"決済 #{trade.position_id} {SIDE_LABELS[trade.side]} {inst.format_qty(trade.qty)}"
                f" @ {inst.format_price(trade.exit_price)}　損益 {fmt_yen(trade.pnl)}"
            )
    for position in positions:
        if position.symbol != inst.key:
            continue
        side, opened_at, entry, qty = entries.get(
            position.id, (position.side, position.opened_at, position.entry_price, 0)
        )
        entries[position.id] = (side, opened_at, entry, qty + position.qty)
        color = UP if position.side == LONG else DOWN
        fig.add_hline(
            y=position.entry_price,
            line={"color": color, "width": 1, "dash": "dot"},
            annotation_text=f"#{position.id} {SIDE_LABELS[position.side]} 建値 {inst.format_price(position.entry_price)}",
            annotation_position="top right",
            annotation_font={"color": color, "size": 11},
            annotation_bgcolor="rgba(127, 127, 127, 0.12)",
            row=1,
            col=1,
        )

    for side, symbol, color, name in ((LONG, "triangle-up", UP, "ロング"), ("short", "triangle-down", DOWN, "ショート")):
        xs, ys, texts = [], [], []
        for position_id, (entry_side, opened_at, entry, qty) in sorted(entries.items()):
            if entry_side != side:
                continue
            i = locate(index, trade_time(opened_at))
            if i is None:
                continue
            xs.append(labels[i])
            ys.append(entry)
            texts.append(f"#{position_id} {name} {inst.format_qty(qty)} @ {inst.format_price(entry)}")
        if xs:
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="markers",
                    name=f"{name}で建てた位置",
                    marker={"symbol": symbol, "size": 13, "color": color, "line": {"width": 1, "color": "white"}},
                    text=texts,
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
    if exits_x:
        fig.add_trace(
            go.Scatter(
                x=exits_x,
                y=exits_y,
                mode="markers",
                name="決済した位置",
                marker={"symbol": "x", "size": 11, "color": EXIT, "line": {"width": 1, "color": "white"}},
                text=exits_text,
                hovertemplate="%{text}<extra></extra>",
            ),
            row=1,
            col=1,
        )


def realized_curve(trades: Sequence[Trade]) -> pd.DataFrame:
    """決済ごとの実現損益と、その累計 (時刻順)。"""
    rows = sorted(((trade_time(t.closed_at), t) for t in trades), key=lambda item: item[0])
    data = {
        "time": [when for when, _ in rows],
        "pnl": [t.pnl for _, t in rows],
        "label": [f"#{t.position_id} {get_instrument(t.symbol).name} {SIDE_LABELS[t.side]}" for _, t in rows],
    }
    df = pd.DataFrame(data)
    df["cumulative"] = df["pnl"].cumsum().round(2) if not df.empty else pd.Series(dtype=float)
    return df


def equity_figure(trades: Sequence[Trade], current_total: float | None, now: datetime) -> go.Figure:
    """通算損益の推移。決済のたびに実現損益の累計が階段状に変わり、最後に評価損益を含む現在の通算損益を置く。"""
    curve = realized_curve(trades)
    fig = go.Figure()
    if not curve.empty:
        start = curve["time"].iloc[0] - timedelta(minutes=1)
        xs = [start, *curve["time"]]
        ys = [0.0, *curve["cumulative"]]
        texts = ["開始", *[f"{label}　{fmt_yen(pnl)}" for label, pnl in zip(curve["label"], curve["pnl"])]]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines+markers",
                line={"shape": "hv", "color": "#5b6b80", "width": 2},
                marker={"size": 7, "color": ["#5b6b80", *[UP if v > 0 else DOWN for v in curve["pnl"]]]},
                name="実現損益の累計",
                text=texts,
                hovertemplate="%{x|%Y/%m/%d %H:%M}<br>%{text}<br>累計 %{y:+,.0f}円<extra></extra>",
            )
        )
    if current_total is not None:
        fig.add_trace(
            go.Scatter(
                x=[now],
                y=[current_total],
                mode="markers",
                marker={"symbol": "star", "size": 15, "color": UP if current_total >= 0 else DOWN},
                name="現在 (評価損益を含む)",
                hovertemplate="現在の通算損益 %{y:+,.0f}円<extra></extra>",
            )
        )
    fig.add_hline(y=0, line={"color": EXIT, "width": 1})
    fig.update_layout(
        height=380,
        margin={"l": 8, "r": 8, "t": 36, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
        yaxis={"tickformat": "+,.0f", "ticksuffix": "円", "gridcolor": GRID},
        xaxis={"gridcolor": GRID, "tickformat": "%Y/%m/%d"},
    )
    return fig


def pnl_by_instrument_figure(rows: Sequence[SummaryRow]) -> go.Figure:
    """銘柄別の損益 (実現損益 + 評価損益)。現在値を取得できなかった銘柄は実現損益だけ。"""
    names = [get_instrument(r.symbol).name for r in rows]
    values = [r.total if r.total is not None else r.realized for r in rows]
    fig = go.Figure(
        go.Bar(
            x=names,
            y=values,
            marker={"color": [UP if v >= 0 else DOWN for v in values]},
            text=[fmt_yen(v) for v in values],
            textposition="outside",
            hovertemplate="%{x}<br>%{y:+,.0f}円<extra></extra>",
            cliponaxis=False,
        )
    )
    fig.add_hline(y=0, line={"color": EXIT, "width": 1})
    fig.update_layout(
        height=380,
        margin={"l": 8, "r": 8, "t": 36, "b": 8},
        showlegend=False,
        yaxis={"tickformat": "+,.0f", "ticksuffix": "円", "gridcolor": GRID},
    )
    return fig
