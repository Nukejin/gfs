@echo off
chcp 932 >nul
setlocal
cd /d "%~dp0"
title 取引シミュレーター

rem ZIP の中から直接実行していないか確認する
if not exist "app.py" (
  echo app.py が見つかりません。
  echo ダウンロードした ZIP を右クリックして「すべて展開」を選び、展開したフォルダの中の start_app.bat を実行してください。
  goto :end
)

rem Python 3.10 以上を探す。py ランチャーを優先し、Microsoft Store の「python」ショートカットは使わない
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto :install_python

rem 必要なライブラリが入っていなければインストールする
%PY% -c "import streamlit, plotly, yfinance" >nul 2>&1
if errorlevel 1 (
  echo 必要なライブラリをインストールしています。初回だけ数分かかります。
  %PY% -m pip install -r requirements-app.txt
  if errorlevel 1 (
    echo.
    echo ライブラリのインストールに失敗しました。上に表示されたメッセージを確認してください。
    goto :end
  )
)

rem 初回起動時に Streamlit がメールアドレスを聞いて止まらないようにする
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
  > "%USERPROFILE%\.streamlit\credentials.toml" (
    echo [general]
    echo email = ""
  )
)

echo.
echo 取引シミュレーターを起動します。ブラウザが自動で開きます。
echo 終わるときは、この黒い画面を閉じてください。
echo.
%PY% -m streamlit run app.py
goto :end

:install_python
echo Python 3.10 以上が見つかりません。
echo Python をインストールしてから、もう一度 start_app.bat をダブルクリックしてください。
echo.
where winget >nul 2>&1
if errorlevel 1 goto :open_python_site
choice /c YN /m "今すぐ winget で Python 3.13 をインストールしますか"
if errorlevel 2 goto :open_python_site
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
echo.
echo インストールが終わったら、この画面を閉じて start_app.bat をもう一度ダブルクリックしてください。
goto :end

:open_python_site
echo ブラウザで Python のダウンロードページを開きます。
echo インストーラーの最初の画面で「Add python.exe to PATH」にチェックを入れてから「Install Now」を押してください。
start "" https://www.python.org/downloads/

:end
echo.
pause
