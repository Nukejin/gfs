"""Google Colab / Jupyter Notebook 用の取引画面 (ipywidgets)。

ノートブックのセルで次のように実行すると、注文・決済・損益確認ができる画面が出る。

    from trade_sim.notebook import show
    app = show("portfolio.json")
"""

from __future__ import annotations

import html
from collections.abc import Callable

import ipywidgets as widgets
from IPython.display import display

from .instruments import INSTRUMENTS, LEVERAGE_CHOICES, Instrument, format_leverage, get_instrument
from .portfolio import LONG, SHORT, SIDE_LABELS
from .prices import PriceSource
from .simulator import USER_ERRORS, Result, Simulator, Table, parse_date

# 銘柄を選んだときの数量の初期値
DEFAULT_QTY = {"stock": 100, "fx": 10000}

# Colab のライト/ダーク両方で読めるよう、文字色は指定せず半透明の線だけ使う
_LINE = "rgba(127, 127, 127, 0.35)"
_ERROR = "#d9534f"


# ---- HTML の組み立て -----------------------------------------------------


def table_html(table: Table) -> str:
    def cells(row: list[str], tag: str, extra: str = "") -> str:
        out = []
        for text, align in zip(row, table.aligns):
            style = (
                f"padding: 4px 10px; border-bottom: 1px solid {_LINE}; white-space: nowrap;"
                f" text-align: {'right' if align == '>' else 'left'};{extra}"
            )
            out.append(f'<{tag} style="{style}">{html.escape(text)}</{tag}>')
        return "".join(out)

    parts = [
        '<div style="overflow-x: auto; max-width: 100%;">',
        # color / font-size: inherit は、互換モードの表示でも表がまわりの文字色・大きさを引き継ぐようにするため
        '<table style="border-collapse: collapse; color: inherit; font-size: inherit;'
        ' font-variant-numeric: tabular-nums; margin: 2px 0 6px;">',
        f"<thead><tr>{cells(table.headers, 'th', ' font-weight: 600;')}</tr></thead>",
        "<tbody>",
        *(f"<tr>{cells(row, 'td')}</tr>" for row in table.rows),
        "</tbody>",
    ]
    if table.footer:
        footer_style = f" font-weight: 600; border-top: 2px solid {_LINE};"
        parts += ["<tfoot>", *(f"<tr>{cells(row, 'td', footer_style)}</tr>" for row in table.footer), "</tfoot>"]
    parts += ["</table>", "</div>"]
    return "".join(parts)


def result_html(result: Result, emphasize: bool = False) -> str:
    """操作結果を HTML にする。emphasize=True なら最初の行 (通算損益など) を大きく表示する。"""
    parts = []
    if result.table is not None:
        parts.append(table_html(result.table))
    for i, line in enumerate(result.lines):
        style = "font-size: 1.2em; font-weight: 700; margin: 4px 0;" if emphasize and i == 0 else "margin: 2px 0;"
        parts.append(f'<div style="{style}">{html.escape(line)}</div>')
    for note in result.notes:
        parts.append(f'<div style="margin: 2px 0; opacity: 0.75; font-size: 0.92em;">※ {html.escape(note)}</div>')
    return "".join(parts)


def error_html(message: str) -> str:
    return f'<div style="color: {_ERROR}; font-weight: 600; margin: 2px 0;">エラー: {html.escape(message)}</div>'


def _info_html(message: str) -> str:
    return f'<div style="margin: 2px 0; opacity: 0.75;">{html.escape(message)}</div>'


def _heading(text: str) -> widgets.HTML:
    style = f"font-size: 1.1em; font-weight: 700; margin: 16px 0 2px; padding-bottom: 2px; border-bottom: 1px solid {_LINE};"
    return widgets.HTML(f'<div style="{style}">{html.escape(text)}</div>')


def _leverage_options(inst: Instrument) -> list[tuple[str, float]]:
    return [(format_leverage(x), x) for x in LEVERAGE_CHOICES[inst.kind]]


def _row(*children: widgets.Widget) -> widgets.HBox:
    return widgets.HBox(list(children), layout=widgets.Layout(flex_flow="row wrap", align_items="center"))


# ---- 画面 ----------------------------------------------------------------


class TradeApp:
    def __init__(self, simulator: Simulator) -> None:
        self.sim = simulator
        label = {"description_width": "5.5em"}
        first = INSTRUMENTS[0]

        # 現在値
        self.status = widgets.HTML()
        self.quote_view = widgets.HTML()
        self.refresh_button = widgets.Button(description="価格を更新", icon="refresh")

        # 新規注文
        self.symbol = widgets.Dropdown(
            options=[(inst.name, inst.key) for inst in INSTRUMENTS], value=first.key, description="銘柄", style=label
        )
        self.side = widgets.ToggleButtons(
            options=[("ロング（買い）", LONG), ("ショート（売り）", SHORT)], value=LONG, description="売買", style=label
        )
        self.qty = widgets.IntText(
            value=DEFAULT_QTY[first.kind], description="数量", style=label, layout=widgets.Layout(width="15em")
        )
        self.unit = widgets.HTML(first.unit)
        self.leverage = widgets.Dropdown(
            options=_leverage_options(first), value=1.0, description="レバレッジ", style=label,
            layout=widgets.Layout(width="15em"),
        )
        self.leverage_hint = widgets.HTML(f"{format_leverage(first.max_leverage)}まで")
        self.order_date = widgets.Text(
            placeholder="空欄なら最新の価格（例: 2025-04-01）",
            description="約定日",
            style=label,
            layout=widgets.Layout(width="26em"),
        )
        self.order_button = widgets.Button(description="発注する", button_style="primary")
        self.order_message = widgets.HTML()

        # 決済
        self.positions_view = widgets.HTML()
        self.close_target = widgets.Dropdown(description="建玉", style=label, layout=widgets.Layout(width="30em"))
        self.close_qty = widgets.IntText(value=0, description="数量", style=label, layout=widgets.Layout(width="15em"))
        self.close_date = widgets.Text(
            placeholder="空欄なら最新の価格", description="決済日", style=label, layout=widgets.Layout(width="26em")
        )
        self.close_button = widgets.Button(description="決済する", button_style="warning")
        self.close_all_button = widgets.Button(description="すべて決済")
        self.close_message = widgets.HTML()

        # 損益・履歴
        self.summary_view = widgets.HTML()
        self.history_view = widgets.HTML()

        self._busy = False
        self._has_positions = False
        self._confirming_close_all = False

        self.symbol.observe(self._on_symbol_change, names="value")
        self.refresh_button.on_click(self._on_refresh)
        self.order_button.on_click(self._on_order)
        self.close_button.on_click(self._on_close)
        self.close_all_button.on_click(self._on_close_all)

        self.widget = widgets.VBox(
            [
                widgets.HTML('<div style="font-size: 1.4em; font-weight: 700;">株・為替 取引シミュレーター</div>'),
                self.status,
                _heading("損益"),
                self.summary_view,
                _heading("現在値"),
                self.quote_view,
                self.refresh_button,
                _heading("新規注文"),
                self.symbol,
                self.side,
                _row(self.qty, self.unit),
                _row(self.leverage, self.leverage_hint),
                self.order_date,
                self.order_button,
                self.order_message,
                _heading("保有中の建玉"),
                self.positions_view,
                self.close_target,
                _row(self.close_qty, widgets.HTML("0 ならすべて決済")),
                self.close_date,
                _row(self.close_button, self.close_all_button),
                self.close_message,
                _heading("取引履歴"),
                self.history_view,
            ]
        )
        self.refresh()

    def refresh(self) -> None:
        """表示をすべて最新の状態にする。"""
        views: list[tuple[widgets.HTML, Callable[[], str]]] = [
            (self.summary_view, lambda: result_html(self.sim.summary(), emphasize=True)),
            (self.quote_view, lambda: result_html(self.sim.quote())),
            (self.positions_view, lambda: result_html(self.sim.positions())),
            (self.history_view, lambda: result_html(self.sim.history())),
        ]
        for view, render in views:
            try:
                view.value = render()
            except USER_ERRORS as e:
                view.value = error_html(str(e))
        self._update_close_targets()
        self.status.value = _info_html(
            f"最終更新: {self.sim.clock():%Y-%m-%d %H:%M}（日本時間）　データ: {self.sim.path}"
        )

    # ---- イベント --------------------------------------------------------

    def _on_symbol_change(self, change: dict) -> None:
        inst = get_instrument(change["new"])
        self.qty.value = DEFAULT_QTY[inst.kind]
        self.unit.value = inst.unit
        self.leverage.options = _leverage_options(inst)
        self.leverage.value = 1.0
        self.leverage_hint.value = f"{format_leverage(inst.max_leverage)}まで"

    def _on_refresh(self, _button: widgets.Button) -> None:
        self._cancel_close_all()
        self._set_busy(True)
        try:
            self.sim.refresh_prices()
            self.refresh()
        finally:
            self._set_busy(False)

    def _on_order(self, _button: widgets.Button) -> None:
        self._cancel_close_all()
        symbol, side, qty, day_text = self.symbol.value, self.side.value, self.qty.value, self.order_date.value
        leverage = float(self.leverage.value)
        self._run(
            self.order_message, lambda: self.sim.open(symbol, side, qty, day=parse_date(day_text), leverage=leverage)
        )

    def _on_close(self, _button: widgets.Button) -> None:
        self._cancel_close_all()
        target = self.close_target.value
        if target is None:
            self.close_message.value = error_html("決済する建玉を選んでください。")
            return
        qty = self.close_qty.value or None  # 0 ならすべて決済
        day_text = self.close_date.value
        self._run(self.close_message, lambda: self.sim.close([target], qty=qty, day=parse_date(day_text)))

    def _on_close_all(self, _button: widgets.Button) -> None:
        if not self._confirming_close_all:
            self._confirming_close_all = True
            self.close_all_button.description = "もう一度押すと全決済"
            self.close_all_button.button_style = "danger"
            self.close_message.value = _info_html(
                "保有中の建玉をすべて決済します。よければ「もう一度押すと全決済」を押してください。"
            )
            return
        self._cancel_close_all()
        day_text = self.close_date.value
        self._run(self.close_message, lambda: self.sim.close(["all"], day=parse_date(day_text)))

    # ---- 内部 ------------------------------------------------------------

    def _run(self, message: widgets.HTML, action: Callable[[], Result]) -> None:
        """ボタンの操作を実行して、結果かエラーを message に表示する。"""
        self._set_busy(True)
        message.value = _info_html("処理中…")
        try:
            result = action()
        except USER_ERRORS as e:
            message.value = error_html(str(e))
        except Exception as e:  # ボタンから呼ばれるので、想定外のエラーも画面に出す
            message.value = error_html(f"予期しないエラーが発生しました ({type(e).__name__}: {e})")
        else:
            message.value = result_html(result)
            self.refresh()
        finally:
            self._set_busy(False)

    def _cancel_close_all(self) -> None:
        if self._confirming_close_all:
            self._confirming_close_all = False
            self.close_all_button.description = "すべて決済"
            self.close_all_button.button_style = ""

    def _update_close_targets(self) -> None:
        try:
            positions = self.sim.load().positions
        except USER_ERRORS:
            positions = []
        options = []
        for p in positions:
            inst = get_instrument(p.symbol)
            label = (
                f"#{p.id} {inst.name} {SIDE_LABELS[p.side]} {inst.format_qty(p.qty)}"
                f" @ {inst.format_price(p.entry_price)}"
            )
            options.append((label, p.id))
        current = self.close_target.value
        self.close_target.options = options
        values = [value for _, value in options]
        if values:
            # 選んでいた建玉が残っていればそのまま、なければ先頭を選ぶ (空の状態から増えたときは未選択になるため)
            self.close_target.value = current if current in values else values[0]
        self._has_positions = bool(options)
        self._update_buttons()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.refresh_button.disabled = self._busy
        self.order_button.disabled = self._busy
        can_close = self._has_positions and not self._busy
        self.close_target.disabled = not self._has_positions
        self.close_button.disabled = not can_close
        self.close_all_button.disabled = not can_close


def _enable_colab_widgets() -> None:
    """Colab で ipywidgets 8 以降を使う場合は、Colab のカスタムウィジェット表示を有効にする。"""
    try:
        from google.colab import output  # type: ignore[import-not-found]
    except ImportError:
        return
    if int(widgets.__version__.split(".")[0]) >= 8:
        output.enable_custom_widget_manager()


def show(data_file: str = "portfolio.json", source: PriceSource | None = None) -> TradeApp:
    """取引画面を表示する。data_file は取引データの保存先。"""
    _enable_colab_widgets()
    app = TradeApp(Simulator(data_file, source))
    display(app.widget)
    return app
