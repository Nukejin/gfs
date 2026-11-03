# trade_sim — 株・為替の取引シミュレーター

[yfinance](https://github.com/ranaroussi/yfinance)（Yahoo Finance）の価格を使って、ロング（買い）とショート（売り）の取引を仮想的に行い、損益を記録するコマンドラインツールです。

| 銘柄 | 指定のしかた | ティッカー | 数量の単位 |
|---|---|---|---|
| キオクシア | `kioxia` / `285A` / `キオクシア` | 285A.T | 株 |
| レーザーテック | `lasertec` / `6920` / `レーザーテック` | 6920.T | 株 |
| ドル円 | `usdjpy` / `USD/JPY` / `ドル円` | USDJPY=X | 通貨 |
| ユーロ円 | `eurjpy` / `EUR/JPY` / `ユーロ円` | EURJPY=X | 通貨 |

## セットアップ

Python 3.10 以上が必要です（yfinance が使うライブラリの要件）。

```bash
pip install -r requirements.txt
```

## 使い方

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

## 損益の計算方法

対象はすべて円建てなので、損益も円で計算します。

- ロング: (決済値 − 建値) × 数量
- ショート: (建値 − 決済値) × 数量

例えば、ドル円を 10,000 通貨ショートして 1 円下がると +10,000 円です。

## データの保存先

建玉と取引履歴は、カレントディレクトリの `portfolio.json` に保存されます。保存先は `--file` オプション（例: `python -m trade_sim --file my.json summary`）か、環境変数 `TRADE_SIM_FILE` で変更できます。
`python -m trade_sim reset` で全データを削除できます。

## 注意事項

- 価格は Yahoo Finance の日足データです。取引時間中は当日の途中経過（東証銘柄はおよそ 20 分遅れ）、時間外は直近の終値になります。
- yfinance は Yahoo Finance の非公式ライブラリなので、仕様変更やアクセス制限で一時的に取得できなくなることがあります。そのときは `pip install -U yfinance` で更新してみてください。
- 手数料、スプレッド、FX のスワップポイント、信用取引の金利・貸株料、証拠金・ロスカットは計算に含めていません。

## テスト

```bash
python -m unittest
```

テストでは実際の価格を取得せず、テスト用の価格データを使います。
