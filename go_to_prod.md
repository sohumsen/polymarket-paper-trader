# Go To Prod: Polymarket Real Money

## Status
- Market data: REAL (Polymarket Gamma API, no auth needed)
- LLM analysis: REAL (Claude CLI)
- Trade execution: PAPER (needs replacing)
- Auth/wallet: NONE (needs adding)

---

## Step 1: Wallet (5 min)

```python
from eth_account import Account
acct = Account.create()
print(acct.key.hex())  # save as POLYMARKET_PRIVATE_KEY
print(acct.address)    # your deposit address
```

Store key in `.env` (never commit):
```
POLYMARKET_PRIVATE_KEY=0x...
```

---

## Step 2: Fund Wallet (30 min + KYC wait)

1. Open Coinbase, complete KYC if not done
2. Buy USDC with Monzo debit card
3. Withdraw → select **Polygon network** → paste wallet address
4. Start small: £50–100 to test the execution layer

---

## Step 3: VPN

- **ProtonVPN** free tier — Irish servers (Ireland is not geo-blocked)
- Enable kill switch so bot halts if VPN drops (paid plan only)
- For real money: ProtonVPN Plus (~£4/mo) is worth it for kill switch alone

---

## Step 4: One-time Contract Approval

Polymarket requires approving their CLOB contracts to spend your USDC.  
The `py-clob-client` handles this:

```python
from py_clob_client.client import ClobClient

client = ClobClient(
    "https://clob.polymarket.com",
    key=os.environ["POLYMARKET_PRIVATE_KEY"],
    chain_id=137  # Polygon
)
client.create_or_derive_api_creds()  # one-time: sets up API credentials
```

Run once manually before starting the bot.

---

## Step 5: Code Changes in engine.py

### Install dependency
```
pip install py-clob-client
```

### Add at top of engine.py
```python
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

CLOB_HOST = "https://clob.polymarket.com"

def get_clob_client():
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")
    return ClobClient(CLOB_HOST, key=key, chain_id=137)
```

### Add geoblock check (call before any order)
```python
def check_geoblock():
    r = requests.get("https://polymarket.com/api/geoblock", timeout=5)
    if r.json().get("blocked"):
        raise RuntimeError("IP blocked — VPN may have dropped")
```

### Replace execute_trade() body
```python
def execute_trade(state, opp, size, config=None):
    check_geoblock()

    client = get_clob_client()

    # token_id is the YES or NO outcome token from the market
    token_id = opp["clob_token_id"]  # see note below
    price = opp["entry_price"]
    
    order = client.create_order(OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side="BUY",
        order_type=OrderType.GTC,
    ))
    resp = client.post_order(order)

    trade = {
        "market_id": opp["market_id"],
        "order_id": resp.get("orderID"),
        "side": opp["side"],
        "entry_price": price,
        "shares": size / price,
        "cost": size,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        # copy other opp fields as before
    }
    state["trades"].append(trade)
    save_state(state)
    return trade
```

### Getting token_id from market data
The Gamma API already returns `clobTokenIds` per market. Parse it where you build `opp`:
```python
clob_ids = json.loads(market.get("clobTokenIds", "[]"))
# clob_ids[0] = YES token, clob_ids[1] = NO token
opp["clob_token_id"] = clob_ids[0] if side == "YES" else clob_ids[1]
```

### Seed real balance at startup
```python
def get_real_balance():
    from web3 import Web3
    USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    abi = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
    contract = w3.eth.contract(address=USDC_POLYGON, abi=abi)
    address = os.environ["POLYMARKET_WALLET_ADDRESS"]
    raw = contract.functions.balanceOf(address).call()
    return raw / 1e6  # USDC has 6 decimals
```

---

## Step 6: Safety Limits (already in engine, verify these are set)

- `max_position_pct`: max % of balance per trade — keep at 2% to start
- `max_daily_trades`: hard cap on trades per day
- `min_edge`: minimum edge required to trade — keep high (0.08+) initially

---

## Checklist Before First Live Trade

- [ ] Wallet created, address noted
- [ ] USDC on Polygon confirmed (check polygonscan.com)
- [ ] Contract approval run once
- [ ] VPN connected, Irish server
- [ ] `POLYMARKET_PRIVATE_KEY` in `.env`
- [ ] `max_position_pct` set to 0.02 (2%)
- [ ] `min_edge` set to 0.08+
- [ ] Geoblock check tested manually
- [ ] Run one manual test trade via `/api/trade` endpoint before enabling bot autopilot

---

## What Does NOT Change

- Market fetching logic
- LLM ensemble analysis
- Kelly sizing
- Calibration system
- Frontend dashboard
- Server/API structure

The entire real-money swap is localized to `execute_trade()` + a client init block.
