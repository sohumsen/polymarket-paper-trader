#!/bin/bash
echo ""
echo "  ============================================"
echo "  POLYMARKET PAPER TRADER"
echo "  ============================================"
echo "  Starting server on http://localhost:8000"
echo "  Bot will auto-start scanning and trading"
echo "  Press Ctrl+C to stop"
echo "  ============================================"
echo ""
cd "$(dirname "$0")"
python server.py
