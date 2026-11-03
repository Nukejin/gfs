# trade_sim — 株・為替の取引シミュレーター

[yfinance](https://github.com/ranaroussi/yfinance)（Yahoo Finance）の価格を使って、ロング（買い）とショート（売り）の取引を仮想的に行い、損益を記録するコマンドラインツールです。

| 銘柄 | 指定のしかた | ティッカー | 数量の単位 |
|---|---|---|---|
| キオクシア | `kioxia` / `285A` / `キオクシア` | 285A.T | 株 |
| レーザーテック | `lasertec` / `6920` / `レーザーテック` | 6920.T | 株 |
| ドル円 | `usdjpy` / `USD/JPY` / `ドル円` | USDJPY=X | 通貨 |
| ユーロ円 | `eurjpy` / `EUR/JPY` / `ユーロ円` | EURJPY=X | 通貨 |

## PC でチャート付きのアプリを使う（おすすめ）

PC で動かして、ブラウザで操作するアプリです。ローソク足チャートを見ながら注文・決済ができます。

- ローソク足チャート（5分足・15分足・1時間足・日足・週足）、移動平均線、出来高
- 自分が建てた位置（▲ロング / ▼ショート）と決済した位置（×）、保有中の建値をチャート上に表示
- 通算損益・実現損益・評価損益・勝率、通算損益の推移と銘柄別の損益のグラフ
- 価格の自動更新（30秒・1分・5分ごと）、取引履歴の CSV 保存
- 色は日本の証券会社の表示に合わせて、値上がり・プラスが赤、値下がり・マイナスが青

### 起動のしかた

Python 3.10 以上が必要です。

```bash
pip install -r requirements-app.txt
streamlit run app.py
```

#### Windows の場合

1. Python が入っていなければ、[python.org](https://www.python.org/downloads/) からインストールします。インストーラーの最初の画面で「Add python.exe to PATH」にチェックを入れてから「Install Now」を押してください。
   - Windows に最初からある「python」コマンドは Microsoft Store へのショートカットで、Python 本体ではありません。
2. GitHub からダウンロードした ZIP は、右クリック →「すべて展開」で展開します。
3. 展開したフォルダの `start_app.bat` をダブルクリックします。初回は必要なライブラリを自動でインストールし、ブラウザで `http://localhost:8501` が開きます。

Python が見つからないときは、`start_app.bat` がその場で winget を使ったインストールを提案します。終わるときは黒い画面（コマンドプロンプト）を閉じてください。

取引データはコマンドライン版と同じ `portfolio.json` に保存されるので、どちらからでも続きを操作できます。

## ブラウザで使う（Google Colab）

インストールなしで、ブラウザ上の Google Colab で動かせます。ボタンやプルダウンで注文・決済ができる画面が出ます。

1. [`colab/trade_sim.ipynb`](colab/trade_sim.ipynb) を Google ドライブに置いて、Google Colaboratory で開きます（Colab のメニュー「ファイル → ノートブックをアップロード」でも開けます）。
2. メニューの「ランタイム → すべてのセルを実行」を選びます。
3. Google ドライブへのアクセスを許可すると、取引データがドライブの `trade_sim/portfolio.json` に保存され、次回も続きから使えます。

ノートブックにはシミュレーター本体のコードが埋め込まれているので、ノートブック 1 つだけで動きます。

## コマンドラインで使う

### セットアップ

Python 3.10 以上が必要です（yfinance が使うライブラリの要件）。

```bash
pip install -r requirements.txt
```

### 使い方

```bash
# 現在値と前日比を表示
python -m trade_sim quote

# 新規にロング（買い）/ ショート（売り）を建てる（最新の価格で約定）
python -m trade_sim long kioxia 100        # キオクシアを 100 株買い
python -m trade_sim short usdjpy 10000     # ドル円を 1 万通貨売り

# 保有中の建玉と評価損益（含み損益）
python -m trade_sim positions

# 決済する（建玉 ID は positions で確認）
python -m trade_sim close 1                # 建玉 #1 をすべて決済
python -m trade_sim close 2 --qty 5000     # 建玉 #2 を 5,000 通貨だけ決済
python -m trade_sim close all              # すべての建玉を決済

# 決済済みの取引履歴
python -m trade_sim history
python -m trade_sim history lasertec       # 銘柄で絞り込み

# 銘柄別・通算の損益（実現損益 + 評価損益）
python -m trade_sim summary
```

`summary` では銘柄ごとに決済回数、勝率、実現損益、保有中の建玉数、評価損益、合計を表示し、最後に**通算損益**（実現損益と評価損益の合計）を表示します。

### 過去の価格でシミュレーションする

`--date` を付けると、その日の終値で約定します。休場日を指定した場合は直前の営業日の終値、当日を指定した場合はその時点の最新値を使います。

```bash
python -m trade_sim long lasertec 100 --date 2025-04-01
python -m trade_sim close 1 --date 2025-06-30
```

`--price` で約定価格を直接指定することもできます（例: `--price 152.5`）。

### データの保存先

建玉と取引履歴は、カレントディレクトリの `portfolio.json` に保存されます。保存先は `--file` オプション（例: `python -m trade_sim --file my.json summary`）か、環境変数 `TRADE_SIM_FILE` で変更できます。
`python -m trade_sim reset` で全データを削除できます。

## 損益の計算方法

対象はすべて円建てなので、損益も円で計算します。

- ロング: (決済値 − 建値) × 数量
- ショート: (建値 − 決済値) × 数量

例えば、ドル円を 10,000 通貨ショートして 1 円下がると +10,000 円です。

## 注意事項

- 取引の時刻は日本時間で記録します。
- 価格は Yahoo Finance の日足データです。取引時間中は当日の途中経過（東証銘柄はおよそ 20 分遅れ）、時間外は直近の終値になります。
- yfinance は Yahoo Finance の非公式ライブラリなので、仕様変更やアクセス制限で一時的に取得できなくなることがあります。そのときは `pip install -U yfinance` で更新してみてください。
- 手数料、スプレッド、FX のスワップポイント、信用取引の金利・貸株料、証拠金・ロスカットは計算に含めていません。

## 開発

```bash
python -m unittest                   # テスト (実際の価格は取得せず、テスト用の価格データを使います)
python tools/build_notebook.py       # trade_sim を変更したら、Colab 用ノートブックを作り直す
```

画面のテストには `requirements-app.txt` のライブラリと `ipywidgets` が必要です（入っていなければ省略されます）。
