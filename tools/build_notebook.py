"""Google Colab 用のノートブック colab/trade_sim.ipynb を作る。

trade_sim パッケージのソースをノートブックに埋め込むので、ノートブック 1 つだけで動く
(GitHub やファイルのアップロードは不要)。パッケージを変更したら作り直すこと。

    python tools/build_notebook.py          # 作り直す
    python tools/build_notebook.py --check  # 最新かどうか確認する (古ければ終了コード 1)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "trade_sim"
NOTEBOOK_PATH = ROOT / "colab" / "trade_sim.ipynb"

INTRO = """\
# 株・為替 取引シミュレーター

yfinance（Yahoo Finance）の価格を使って、**キオクシア・レーザーテック・ドル円・ユーロ円**のロング（買い）とショート（売り）を仮想的に取引し、損益を記録します。

## 使い方

1. メニューの **ランタイム → すべてのセルを実行** を選びます（または上から順に ▶ を押します）。
2. Google ドライブへのアクセスを求められたら許可してください。取引データはドライブの `trade_sim/portfolio.json` に保存され、次に開いたときも続きから使えます。
3. ②のセルの下に表示される画面で、銘柄・売買・数量を選んで **発注する** を押します。決済は「保有中の建玉」で行います。

- 約定日（YYYY-MM-DD）を入れると、その日の終値で約定します（過去の価格でのシミュレーション）。空欄なら最新の価格です。
- 価格は Yahoo Finance のデータです。東証の銘柄はおよそ 20 分遅れです。
- 手数料、スプレッド、スワップポイント、信用取引の金利・貸株料は計算に含めていません。
"""

SETUP = '''\
#@title ① セットアップ（最初に実行してください） { display-mode: "form" }
#@markdown **save_to_drive**: チェックを入れておくと取引データを Google ドライブに保存し、次に開いたときも続きから使えます。
save_to_drive = True  #@param {type:"boolean"}

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path


def _version(package):
    try:
        return tuple(int(x) for x in re.findall(r"\\d+", importlib.metadata.version(package))[:3])
    except importlib.metadata.PackageNotFoundError:
        return ()


# yfinance が入っていないか古ければ、最新版を入れる
if _version("yfinance") < (1, 7, 0):
    print("yfinance をインストールしています…")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yfinance"], check=True)

# シミュレーター本体 (trade_sim パッケージ) を書き出す
__SOURCES__

_package_dir = Path.cwd() / "trade_sim_app"
for _name, _text in _SOURCES.items():
    _path = _package_dir / _name
    _path.parent.mkdir(parents=True, exist_ok=True)
    _path.write_text(_text, encoding="utf-8")
if str(_package_dir) not in sys.path:
    sys.path.insert(0, str(_package_dir))
for _module in [m for m in sys.modules if m == "trade_sim" or m.startswith("trade_sim.")]:
    del sys.modules[_module]  # セルを実行し直したときに、書き出したコードを読み込み直す

# 取引データの保存先
DATA_FILE = str(Path.cwd() / "portfolio.json")
if save_to_drive:
    try:
        from google.colab import drive
    except ImportError:  # Colab 以外 (手元の Jupyter など) ではカレントディレクトリに保存する
        pass
    else:
        drive.mount("/content/drive")
        DATA_FILE = "/content/drive/MyDrive/trade_sim/portfolio.json"
else:
    print("※ 取引データはこのランタイムの中だけに保存されます (ランタイムが終了すると消えます)。")

print("準備ができました。取引データの保存先:", DATA_FILE)
'''

SHOW = '''\
#@title ② 取引画面を表示 { display-mode: "form" }
#@markdown 下に表示される画面で注文・決済ができます。「価格を更新」を押すと最新の価格を取り直します。
from trade_sim.notebook import show

_app = show(DATA_FILE)
'''

RESET = '''\
#@title （必要なときだけ）取引データをすべて削除 { display-mode: "form" }
#@markdown 建玉と取引履歴をすべて消して、最初からやり直します。**delete_all** にチェックを入れてから、このセルを実行してください。
delete_all = False  #@param {type:"boolean"}

from trade_sim.simulator import Simulator

if delete_all:
    print(*Simulator(DATA_FILE).reset().lines)
else:
    print("削除するときは delete_all にチェックを入れてから、このセルを実行してください。")
'''


def package_sources() -> dict[str, str]:
    sources = {}
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        # r'''...''' で埋め込むので、''' を含むものと、バックスラッシュで終わるものは埋め込めない
        if "'''" in text or text.endswith("\\"):
            raise ValueError(f"{path} はノートブックに埋め込めません")
        sources[f"trade_sim/{path.name}"] = text
    return sources


def sources_literal(sources: dict[str, str]) -> str:
    lines = ["_SOURCES = {"]
    for name, text in sources.items():
        lines.append(f"    {name!r}: r'''{text}''',")
    lines.append("}")
    return "\n".join(lines)


def _cell_source(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines:
        lines[-1] = lines[-1].rstrip("\n")
    return lines


def _code_cell(cell_id: str, text: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {"cellView": "form", "id": cell_id},
        "outputs": [],
        "source": _cell_source(text),
    }


def build() -> str:
    setup = SETUP.replace("__SOURCES__", sources_literal(package_sources()))
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 0,
        "metadata": {
            "colab": {"name": "trade_sim.ipynb", "provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "cells": [
            {"cell_type": "markdown", "metadata": {"id": "intro"}, "source": _cell_source(INTRO)},
            _code_cell("setup", setup),
            _code_cell("app", SHOW),
            _code_cell("reset", RESET),
        ],
    }
    return json.dumps(notebook, ensure_ascii=False, indent=1) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="作り直さずに、最新かどうかだけ確認する")
    args = parser.parse_args()
    content = build()
    if args.check:
        current = NOTEBOOK_PATH.read_text(encoding="utf-8") if NOTEBOOK_PATH.exists() else ""
        if current != content:
            print(f"{NOTEBOOK_PATH.relative_to(ROOT)} が古くなっています。python tools/build_notebook.py で作り直してください。")
            return 1
        print("ノートブックは最新です。")
        return 0
    NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTEBOOK_PATH.write_text(content, encoding="utf-8")
    print(f"{NOTEBOOK_PATH.relative_to(ROOT)} を作りました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
