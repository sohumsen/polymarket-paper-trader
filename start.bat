@echo off
echo.
echo   ============================================
echo   POLYMARKET PAPER TRADER
echo   ============================================
echo   Starting server on http://localhost:8000
echo   Bot will auto-start scanning and trading
echo   Press Ctrl+C to stop
echo   ============================================
echo.
cd /d "%~dp0"
start http://localhost:8000
python server.py
