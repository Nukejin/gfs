@echo off
rem Trade simulator: double-click to start (opens in your browser)
cd /d "%~dp0"
python -c "import streamlit, plotly, yfinance" 2>nul || python -m pip install -r requirements-app.txt
python -m streamlit run app.py
pause
