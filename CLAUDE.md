# Polymarket Paper Trader

## Architecture
- `server.py` — FastAPI backend (REST + WebSocket), bot autopilot loop
- `engine.py` — Market fetching, multi-LLM ensemble analysis, Kelly sizing, trade execution, calibration
- `dashboard/` — React + Vite frontend, served as static files from `dashboard/dist/`
- `trades.json` — Persistent portfolio state (balance, trades)
- `calibration.json` — Historical prediction outcomes for Brier/Platt calibration

## Running
```
python server.py          # starts on http://localhost:8000, bot auto-starts
cd dashboard && npm run build  # rebuild frontend
```

## Key patterns
- Bot loop runs as an asyncio task inside the FastAPI lifespan
- LLM calls use `claude --print` via subprocess (5 parallel personas per market)
- All subprocess calls MUST use `stderr=subprocess.DEVNULL` and `_kill_proc_tree()` on timeout (Windows pipe deadlock prevention)
- Engine functions return plain dicts, NOT Pydantic models — do NOT use `response_model=` for endpoints returning engine data (use `responses={200: {"model": ...}}` for docs only)
- `asyncio.CancelledError` is a `BaseException` in Python 3.14 — always catch it explicitly, `except Exception` will NOT catch it
- Kill server processes with `powershell Stop-Process`, NOT `taskkill` from Git Bash (silently fails)

## API tags
Portfolio | Bot Control | Configuration | Trading | Analytics | System

## Tests
None yet. See test coverage grid in the plan file.
