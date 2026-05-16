import { useState, useEffect, useRef, useCallback } from 'react'
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  ReferenceLine,
} from 'recharts'
import './index.css'

// ── API helpers ──────────────────────────────────

async function apiGet(path) {
  try {
    const r = await fetch(path)
    if (!r.ok) throw new Error(`${r.status}`)
    return await r.json()
  } catch (e) {
    console.warn(`GET ${path} failed:`, e.message)
    return null
  }
}

async function apiPost(path, body) {
  try {
    const opts = { method: 'POST' }
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' }
      opts.body = JSON.stringify(body)
    }
    const r = await fetch(path, opts)
    if (!r.ok) throw new Error(`${r.status}`)
    return await r.json()
  } catch (e) {
    console.warn(`POST ${path} failed:`, e.message)
    return null
  }
}

// ── WebSocket hook ───────────────────────────────

function useWebSocket(onStats, onBotStatus) {
  const [events, setEvents] = useState([])
  const wsRef = useRef(null)
  const timerRef = useRef(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    // Don't double-connect
    if (wsRef.current && wsRef.current.readyState <= 1) return

    try {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws`)
      wsRef.current = ws

      ws.onopen = () => {
        console.log('WS connected')
      }

      ws.onmessage = (e) => {
        if (!mountedRef.current) return
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'state_update') {
            onStats(msg.data)
          } else if (msg.type === 'bot_status') {
            onBotStatus(msg.data)
          } else if (msg.type === 'log_history') {
            setEvents(msg.data || [])
          } else {
            setEvents(prev => [...prev.slice(-149), msg])
          }
        } catch {}
      }

      ws.onclose = () => {
        wsRef.current = null
        if (mountedRef.current) {
          timerRef.current = setTimeout(connect, 3000)
        }
      }

      ws.onerror = () => {
        try { ws.close() } catch {}
      }
    } catch {
      timerRef.current = setTimeout(connect, 3000)
    }
  }, [onStats, onBotStatus])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(timerRef.current)
      if (wsRef.current) {
        try { wsRef.current.close() } catch {}
      }
    }
  }, [connect])

  return events
}

// ── Utility ──────────────────────────────────────

function pnlClass(val) {
  return val > 0.005 ? 'green' : val < -0.005 ? 'red' : ''
}

function fmtPnl(val) {
  if (val == null) return '$0.00'
  return `${val >= 0 ? '+' : '-'}$${Math.abs(val).toFixed(2)}`
}

function feedTypeClass(type) {
  if (type === 'trade_placed') return 'trade'
  if (type === 'resolved') return 'win'
  if (type === 'opportunity') return 'edge'
  if (['scan_start', 'analyzing', 'cycle_start'].includes(type)) return 'scan'
  return 'info'
}

function feedTypeLabel(type) {
  return ({
    trade_placed: 'TRADE', resolved: 'RESOLVED', opportunity: 'EDGE',
    scan_start: 'SCAN', analyzing: 'SCAN', cycle_start: 'CYCLE',
    cycle_complete: 'DONE', no_edge: 'SKIP', bot: 'BOT',
    config: 'CONFIG', info: 'INFO', reset: 'RESET', skip: 'SKIP',
  })[type] || 'INFO'
}

// ── Header ───────────────────────────────────────

function Header({ stats, botStatus, onBotAction }) {
  const label = botStatus.paused ? 'Paused' : botStatus.running ? 'Running' : 'Stopped'
  const dotClass = botStatus.paused ? 'paused' : botStatus.running ? 'running' : 'stopped'

  return (
    <div className="header">
      <div className="header-left">
        <h1><span>Polymarket</span> Paper Trader</h1>
        <span className={`status-dot ${dotClass}`} />
        <span style={{ fontSize: 12, color: 'var(--text2)' }}>{label}</span>
      </div>
      {stats ? (
        <div className="header-stats">
          <div className="stat">
            <span className="stat-label">Cash</span>
            <span className="stat-value">${stats.balance?.toFixed(2)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Deployed</span>
            <span className="stat-value blue">${stats.open_cost?.toFixed(2)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">P&L</span>
            <span className={`stat-value ${pnlClass(stats.total_pnl)}`}>
              {fmtPnl(stats.total_pnl)}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Win Rate</span>
            <span className="stat-value">{stats.win_rate?.toFixed(0) ?? 0}%</span>
          </div>
        </div>
      ) : (
        <div className="header-stats">
          <span style={{ color: 'var(--text2)', fontSize: 13 }}>Loading...</span>
        </div>
      )}
      <div className="btn-group">
        <a href="#about" className="btn btn-sm" style={{ textDecoration: 'none' }}>About</a>
        {!botStatus.running ? (
          <button className="btn btn-green" onClick={() => onBotAction('start')}>Start Bot</button>
        ) : botStatus.paused ? (
          <button className="btn btn-green" onClick={() => onBotAction('start')}>Resume</button>
        ) : (
          <button className="btn" onClick={() => onBotAction('pause')}>Pause</button>
        )}
        {botStatus.running && (
          <button className="btn btn-red" onClick={() => onBotAction('stop')}>Stop</button>
        )}
      </div>
    </div>
  )
}

// ── Deploy Bar ───────────────────────────────────

function DeployBar({ stats }) {
  if (!stats) return null
  const pct = Math.min(stats.deployed_pct || 0, 100)
  return (
    <div style={{ padding: '0 0 8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text2)', marginBottom: 2 }}>
        <span>Deployed: {pct.toFixed(0)}%</span>
        <span>{stats.open_count} open positions</span>
      </div>
      <div className="deploy-bar">
        <div className="deploy-bar-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// ── Live Feed ────────────────────────────────────

function LiveFeed({ events }) {
  const ref = useRef(null)
  const autoScroll = useRef(true)

  useEffect(() => {
    if (ref.current && autoScroll.current) {
      ref.current.scrollTop = ref.current.scrollHeight
    }
  }, [events.length])

  const onScroll = () => {
    if (!ref.current) return
    const { scrollTop, scrollHeight, clientHeight } = ref.current
    autoScroll.current = scrollHeight - scrollTop - clientHeight < 60
  }

  return (
    <div className="card" style={{ gridRow: 'span 2' }}>
      <div className="card-title">
        Live Feed
        <span className="count">{events.length}</span>
      </div>
      <div className="feed" ref={ref} onScroll={onScroll}>
        {events.length === 0 ? (
          <div style={{ padding: 20, color: 'var(--text2)', textAlign: 'center' }}>
            Start the bot to see live activity
          </div>
        ) : events.map((e, i) => (
          <div key={i} className="feed-item">
            <span className="feed-ts">{e.ts?.slice(11, 19) || ''}</span>
            <span className={`feed-type ${feedTypeClass(e.type)}`}>
              {feedTypeLabel(e.type)}
            </span>
            <span className="feed-msg">{e.msg}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Portfolio ────────────────────────────────────

function Portfolio({ refreshKey }) {
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    const data = await apiGet('/api/portfolio')
    if (data) setPositions(data)
    setLoading(false)
  }, [])

  // Refresh on mount and when refreshKey changes
  useEffect(() => { refresh() }, [refresh, refreshKey])

  return (
    <div className="card">
      <div className="card-title">
        Portfolio
        <button className="btn btn-sm" onClick={refresh} disabled={loading}>
          {loading ? '...' : 'Refresh'}
        </button>
      </div>
      {positions.length === 0 ? (
        <div style={{ color: 'var(--text2)', fontSize: 13, padding: 10 }}>No open positions</div>
      ) : positions.map((p, i) => (
        <div key={p.market_id || i} className="position">
          <div className="pos-header">
            <span className="pos-question">{p.question}</span>
            <span className={`pos-side ${p.side?.toLowerCase()}`}>{p.side}</span>
          </div>
          <div className="pos-details">
            <span>Entry: <span>${p.entry_price?.toFixed(2)}</span></span>
            <span>Now: <span>{p.current_price != null ? `${(p.current_price * 100).toFixed(0)}%` : '?'}</span></span>
            <span>Cost: <span>${p.cost?.toFixed(2)}</span></span>
            <span className={`pos-pnl ${p.unrealized > 0.01 ? 'up' : p.unrealized < -0.01 ? 'down' : ''}`}>
              {p.unrealized != null ? fmtPnl(p.unrealized) : '?'}
            </span>
            <span>Win: <span>${p.max_payout?.toFixed(2)}</span></span>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Opportunities ────────────────────────────────

function Opportunities({ onTraded }) {
  const [opps, setOpps] = useState([])
  const [busy, setBusy] = useState(null) // market_id of in-progress action

  const refresh = useCallback(async () => {
    const data = await apiGet('/api/opportunities')
    if (data) setOpps(data)
  }, [])

  // Fetch once on mount, then every 15s
  useEffect(() => {
    refresh()
    const iv = setInterval(refresh, 15000)
    return () => clearInterval(iv)
  }, [refresh])

  const trade = async (opp) => {
    setBusy(opp.market_id)
    const amount = Math.max(1, Math.round(opp.edge * opp.confidence * 500) / 10)
    const result = await apiPost('/api/trade', {
      market_id: opp.market_id,
      side: opp.side,
      amount,
    })
    setBusy(null)
    if (result && !result.error) {
      onTraded?.()
      refresh()
    }
  }

  const skip = async (opp) => {
    setBusy(opp.market_id)
    await apiPost(`/api/skip/${opp.market_id}`)
    setOpps(prev => prev.filter(o => o.market_id !== opp.market_id))
    setBusy(null)
  }

  return (
    <div className="card">
      <div className="card-title">
        Opportunities
        <button className="btn btn-sm" onClick={refresh}>Refresh</button>
      </div>
      {opps.length === 0 ? (
        <div style={{ color: 'var(--text2)', fontSize: 13, padding: 10 }}>
          Run a scan to find opportunities
        </div>
      ) : opps.map((o) => (
        <div key={o.market_id} className="opp">
          <div className="opp-header">
            <span className="opp-question">{o.question}</span>
            <span className={`opp-score ${o.score >= 70 ? 'high' : o.score >= 50 ? 'med' : 'low'}`}>
              {o.score}
            </span>
          </div>
          <div className="opp-details">
            <span>Mkt: {(o.market_price_yes * 100).toFixed(0)}%</span>
            <span style={{ color: 'var(--purple)' }}>Claude: {(o.our_estimate * 100).toFixed(0)}%</span>
            <span style={{ color: o.edge > 0.08 ? 'var(--green)' : 'var(--yellow)' }}>
              Edge: {(o.edge * 100).toFixed(0)}%
            </span>
            <span className={o.side === 'YES' ? 'pos-pnl up' : 'pos-pnl down'}>{o.side}</span>
            <span>ROI: {o.roi_pct?.toFixed(0)}%</span>
          </div>
          <div className="opp-actions">
            <button
              className="btn btn-sm btn-green"
              onClick={() => trade(o)}
              disabled={busy === o.market_id}
            >
              {busy === o.market_id ? '...' : 'Trade'}
            </button>
            <button
              className="btn btn-sm btn-red"
              onClick={() => skip(o)}
              disabled={busy === o.market_id}
            >
              Skip
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Settings ─────────────────────────────────────

function Settings() {
  const [cfg, setCfg] = useState(null)
  const [status, setStatus] = useState(null) // 'saving' | 'saved' | 'error'

  useEffect(() => {
    apiGet('/api/config').then(d => { if (d) setCfg(d) })
  }, [])

  const save = async () => {
    setStatus('saving')
    const result = await apiPost('/api/config', cfg)
    setStatus(result ? 'saved' : 'error')
    setTimeout(() => setStatus(null), 2000)
  }

  if (!cfg) return <div className="card"><div className="card-title">Settings</div><div style={{ color: 'var(--text2)', padding: 10 }}>Loading...</div></div>

  const fields = [
    { key: 'min_edge', label: 'Min Edge', step: 0.01 },
    { key: 'min_confidence', label: 'Min Confidence', step: 0.05 },
    { key: 'max_bet_fraction', label: 'Max Bet %', step: 0.01 },
    { key: 'max_deployed_pct', label: 'Max Deploy %', step: 0.05 },
    { key: 'scan_interval_min', label: 'Scan Interval (min)', step: 5 },
    { key: 'max_trades_per_cycle', label: 'Trades / Cycle', step: 1 },
    { key: 'markets_to_scan', label: 'Markets to Scan', step: 5 },
    { key: 'kelly_fraction', label: 'Kelly Fraction', step: 0.05 },
    { key: 'extremize_gamma', label: 'Extremize Gamma', step: 0.1 },
    { key: 'no_side_edge_bonus', label: 'NO Side Bonus', step: 0.005 },
    { key: 'ensemble_passes', label: 'Ensemble Passes', step: 1 },
  ]

  return (
    <div className="card">
      <div className="card-title">
        Settings
        <button className="btn btn-sm btn-blue" onClick={save} disabled={status === 'saving'}>
          {status === 'saving' ? 'Saving...' : status === 'saved' ? 'Saved!' : status === 'error' ? 'Error' : 'Save'}
        </button>
      </div>
      {fields.map(f => (
        <div key={f.key} className="setting">
          <span className="setting-label">{f.label}</span>
          <input
            className="setting-input"
            type="number"
            step={f.step}
            value={cfg[f.key] ?? ''}
            onChange={e => setCfg(prev => ({ ...prev, [f.key]: parseFloat(e.target.value) || 0 }))}
          />
        </div>
      ))}
    </div>
  )
}

// ── History ──────────────────────────────────────

function History({ refreshKey }) {
  const [trades, setTrades] = useState([])

  const refresh = useCallback(async () => {
    const data = await apiGet('/api/history')
    if (data) setTrades(data)
  }, [])

  useEffect(() => { refresh() }, [refresh, refreshKey])

  const wins = trades.filter(t => t.won).length
  const losses = trades.length - wins
  const wr = trades.length > 0 ? (wins / trades.length * 100) : 0

  return (
    <div className="card">
      <div className="card-title">
        Trade History
        <span className="count">{trades.length}</span>
      </div>
      {trades.length > 0 && (
        <>
          <div style={{ display: 'flex', gap: 16, fontSize: 13, marginBottom: 8 }}>
            <span style={{ color: 'var(--green)' }}>{wins}W</span>
            <span style={{ color: 'var(--red)' }}>{losses}L</span>
            <span>{wr.toFixed(0)}% win rate</span>
          </div>
          <div className="winrate-bar">
            <div className="win" style={{ width: `${wr}%` }} />
            <div className="loss" style={{ width: `${100 - wr}%` }} />
          </div>
        </>
      )}
      <div style={{ maxHeight: 250, overflowY: 'auto' }}>
        {trades.length === 0 ? (
          <div style={{ color: 'var(--text2)', fontSize: 13, padding: 10 }}>No resolved trades yet</div>
        ) : [...trades].reverse().slice(0, 20).map((t, i) => (
          <div key={i} className="history-row">
            <span className={`history-badge ${t.won ? 'w' : 'l'}`}>{t.won ? 'W' : 'L'}</span>
            <span className="history-q">{t.question}</span>
            <span className={`history-pnl ${pnlClass(t.profit)}`}>{fmtPnl(t.profit)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Calibration ─────────────────────────────────

function Calibration() {
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    const data = await apiGet('/api/calibration')
    if (data) setReport(data)
    setLoading(false)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  if (!report) return (
    <div className="card">
      <div className="card-title">Calibration</div>
      <div style={{ color: 'var(--text2)', fontSize: 13, padding: 10 }}>Loading...</div>
    </div>
  )

  if (report.status === 'insufficient_data') return (
    <div className="card">
      <div className="card-title">
        Calibration
        <button className="btn btn-sm" onClick={refresh} disabled={loading}>Refresh</button>
      </div>
      <div style={{ color: 'var(--text2)', fontSize: 13, padding: 10 }}>
        Need resolved predictions for calibration.
        <br />Predictions: {report.n || 0} | Resolved: {report.resolved || 0}
      </div>
    </div>
  )

  const beating = report.beating_market
  return (
    <div className="card">
      <div className="card-title">
        Calibration
        <button className="btn btn-sm" onClick={refresh} disabled={loading}>{loading ? '...' : 'Refresh'}</button>
      </div>
      <div style={{ fontSize: 12, fontFamily: 'var(--mono)', display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Our Brier:</span>
          <span style={{ color: report.brier < 0.2 ? 'var(--green)' : report.brier < 0.3 ? 'var(--yellow)' : 'var(--red)' }}>
            {report.brier?.toFixed(4)}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Market Brier:</span>
          <span>{report.market_brier?.toFixed(4)}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Edge vs Market:</span>
          <span style={{ color: beating ? 'var(--green)' : 'var(--red)' }}>
            {beating ? '+' : ''}{report.edge_over_market?.toFixed(4)}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Log Loss:</span>
          <span>{report.log_loss?.toFixed(4)}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>Skill Score:</span>
          <span style={{ color: report.skill_score > 0 ? 'var(--green)' : 'var(--red)' }}>
            {report.skill_score?.toFixed(4)}
          </span>
        </div>
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 4, marginTop: 4, fontSize: 11, color: 'var(--text2)' }}>
          <div>Reliability: {report.reliability?.toFixed(4)} (lower = better)</div>
          <div>Resolution: {report.resolution?.toFixed(4)} (higher = better)</div>
          <div>Platt: a={report.platt_a?.toFixed(3)} b={report.platt_b?.toFixed(3)}</div>
          <div>Samples: {report.n}</div>
        </div>
        {report.diagnosis && (
          <div style={{ marginTop: 4, padding: '4px 6px', background: 'var(--bg2)', borderRadius: 4, fontSize: 11, color: beating ? 'var(--green)' : 'var(--yellow)' }}>
            {report.diagnosis}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Chart Tooltip ───────────────────────────────

function ChartTooltip({ active, payload, label, formatter }) {
  if (!active || !payload?.length) return null
  return (
    <div className="chart-tooltip">
      {label && <div className="chart-tooltip-label">{label}</div>}
      {payload.map((p, i) => (
        <div key={i} className="chart-tooltip-row">
          <span style={{ color: p.color || 'var(--text)' }}>{p.name}:</span>
          <span>{formatter ? formatter(p.value) : p.value}</span>
        </div>
      ))}
      {payload[0]?.payload?.question && (
        <div className="chart-tooltip-sub">{payload[0].payload.question}</div>
      )}
    </div>
  )
}

// ── P&L Chart ───────────────────────────────────

function PnlChart({ data }) {
  if (!data?.pnl_series?.length) return (
    <div className="card chart-card">
      <div className="card-title">P&L Over Time</div>
      <div className="chart-empty">No resolved trades yet</div>
    </div>
  )

  const series = data.pnl_series

  return (
    <div className="card chart-card">
      <div className="card-title">
        P&L Over Time
        <span className={`chart-summary ${data.summary?.total_pnl >= 0 ? 'green' : 'red'}`}>
          {data.summary?.total_pnl >= 0 ? '+' : ''}${data.summary?.total_pnl?.toFixed(2)}
        </span>
      </div>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={series} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
            <defs>
              <linearGradient id="pnlGreen" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#22c55e" stopOpacity={0.3} />
                <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="pnlRed" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#ef4444" stopOpacity={0} />
                <stop offset="100%" stopColor="#ef4444" stopOpacity={0.3} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(42,43,56,0.5)" vertical={false} />
            <XAxis
              dataKey="ts"
              tick={{ fill: '#8b8d9a', fontSize: 10 }}
              tickFormatter={v => v?.slice(5, 10) || ''}
              axisLine={{ stroke: '#2a2b38' }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: '#8b8d9a', fontSize: 10 }}
              tickFormatter={v => `$${v}`}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip content={<ChartTooltip formatter={v => `$${v.toFixed(2)}`} />} />
            <ReferenceLine y={0} stroke="#2a2b38" strokeDasharray="3 3" />
            <Area
              type="monotone"
              dataKey="cumulative"
              name="Cumulative P&L"
              stroke="#22c55e"
              fill="url(#pnlGreen)"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, fill: '#22c55e', stroke: '#0a0b0f', strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// ── Trade Performance Bar Chart ─────────────────

function TradeBarChart({ data }) {
  if (!data?.trade_bars?.length) return (
    <div className="card chart-card">
      <div className="card-title">Trade Performance</div>
      <div className="chart-empty">No resolved trades yet</div>
    </div>
  )

  const bars = data.trade_bars
  const wins = data.summary?.wins || 0
  const losses = data.summary?.losses || 0
  const wr = wins + losses > 0 ? (wins / (wins + losses) * 100).toFixed(0) : 0

  return (
    <div className="card chart-card">
      <div className="card-title">
        Trade Performance
        <span className="chart-summary" style={{ color: 'var(--text2)' }}>
          <span style={{ color: 'var(--green)' }}>{wins}W</span>
          {' / '}
          <span style={{ color: 'var(--red)' }}>{losses}L</span>
          {' '}({wr}%)
        </span>
      </div>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={bars} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(42,43,56,0.5)" vertical={false} />
            <XAxis
              dataKey="question"
              tick={{ fill: '#8b8d9a', fontSize: 9 }}
              axisLine={{ stroke: '#2a2b38' }}
              tickLine={false}
              interval={0}
              angle={-20}
              textAnchor="end"
              height={45}
            />
            <YAxis
              tick={{ fill: '#8b8d9a', fontSize: 10 }}
              tickFormatter={v => `$${v}`}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip content={<ChartTooltip formatter={v => `$${v.toFixed(2)}`} />} />
            <ReferenceLine y={0} stroke="#2a2b38" />
            <Bar dataKey="profit" name="Profit" radius={[3, 3, 0, 0]} maxBarSize={40}>
              {bars.map((entry, i) => (
                <Cell key={i} fill={entry.won ? '#22c55e' : '#ef4444'} fillOpacity={0.85} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

// ── Portfolio Allocation Donut ───────────────────

function AllocationChart({ data }) {
  const allocation = data?.allocation?.filter(a => a.cost > 0) || []
  const totalDeployed = data?.summary?.total_deployed || 0

  if (!allocation.length) return (
    <div className="card chart-card">
      <div className="card-title">Portfolio Allocation</div>
      <div className="chart-empty">No positions</div>
    </div>
  )

  return (
    <div className="card chart-card">
      <div className="card-title">
        Portfolio Allocation
        <span className="chart-summary" style={{ color: 'var(--cyan)' }}>
          ${totalDeployed.toFixed(2)} deployed
        </span>
      </div>
      <div className="chart-container" style={{ position: 'relative' }}>
        <ResponsiveContainer width="100%" height={220}>
          <PieChart>
            <Pie
              data={allocation}
              dataKey="cost"
              nameKey="question"
              cx="50%"
              cy="50%"
              innerRadius={55}
              outerRadius={85}
              paddingAngle={2}
              strokeWidth={0}
            >
              {allocation.map((entry, i) => (
                <Cell key={i} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null
                const d = payload[0].payload
                return (
                  <div className="chart-tooltip">
                    <div className="chart-tooltip-label">{d.question}</div>
                    <div className="chart-tooltip-row">
                      <span>Cost:</span><span>${d.cost.toFixed(2)}</span>
                    </div>
                    {d.side && (
                      <div className="chart-tooltip-row">
                        <span>Side:</span><span>{d.side}</span>
                      </div>
                    )}
                  </div>
                )
              }}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="donut-center">
          <div className="donut-amount">${totalDeployed.toFixed(0)}</div>
          <div className="donut-label">deployed</div>
        </div>
      </div>
      <div className="chart-legend">
        {allocation.map((a, i) => (
          <div key={i} className="chart-legend-item">
            <span className="chart-legend-dot" style={{ background: a.color }} />
            <span className="chart-legend-text">{a.question}</span>
            <span className="chart-legend-value">${a.cost.toFixed(2)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Charts Section ──────────────────────────────

function ChartsSection({ refreshKey }) {
  const [data, setData] = useState(null)

  const refresh = useCallback(async () => {
    const d = await apiGet('/api/charts')
    if (d) setData(d)
  }, [])

  useEffect(() => { refresh() }, [refresh, refreshKey])

  return (
    <div className="grid grid-3">
      <PnlChart data={data} />
      <TradeBarChart data={data} />
      <AllocationChart data={data} />
    </div>
  )
}

// ── Hash Router ─────────────────────────────────

function useHash() {
  const [hash, setHash] = useState(location.hash || '#')
  useEffect(() => {
    const fn = () => setHash(location.hash || '#')
    window.addEventListener('hashchange', fn)
    return () => window.removeEventListener('hashchange', fn)
  }, [])
  return hash
}

// ── About Page ──────────────────────────────────

function AboutPage() {
  return (
    <div className="about-page">
      <div className="about-header">
        <a href="#" className="about-back">Back to Dashboard</a>
        <h1>How the Bot Works</h1>
        <p className="about-subtitle">A research-backed prediction market trading engine</p>
      </div>

      <div className="about-grid">

        {/* Architecture Overview */}
        <div className="about-card about-wide">
          <h2>Architecture</h2>
          <div className="about-flow">
            <div className="about-flow-step">
              <div className="about-flow-num">1</div>
              <div className="about-flow-content">
                <h4>Fetch Markets</h4>
                <p>Pull 40+ active markets from the Polymarket Gamma API across multiple sort orders (volume, recency) for diversity.</p>
              </div>
            </div>
            <div className="about-flow-arrow" />
            <div className="about-flow-step">
              <div className="about-flow-num">2</div>
              <div className="about-flow-content">
                <h4>Ensemble Analysis</h4>
                <p>Each market runs through 3-5 diverse LLM personas. Estimates are combined in log-odds space using Bayesian aggregation.</p>
              </div>
            </div>
            <div className="about-flow-arrow" />
            <div className="about-flow-step">
              <div className="about-flow-num">3</div>
              <div className="about-flow-content">
                <h4>Edge Detection</h4>
                <p>Compare our calibrated estimate to the market price. Only trade when edge exceeds a category-adjusted threshold.</p>
              </div>
            </div>
            <div className="about-flow-arrow" />
            <div className="about-flow-step">
              <div className="about-flow-num">4</div>
              <div className="about-flow-content">
                <h4>Kelly Sizing</h4>
                <p>Position sized using the proper binary Kelly formula at quarter-Kelly fraction. Confidence-weighted, capped at 10% of bankroll.</p>
              </div>
            </div>
            <div className="about-flow-arrow" />
            <div className="about-flow-step">
              <div className="about-flow-num">5</div>
              <div className="about-flow-content">
                <h4>Calibrate</h4>
                <p>Every prediction is logged. After 30+ resolutions, Platt scaling auto-corrects systematic bias. Brier decomposition diagnoses what to fix.</p>
              </div>
            </div>
          </div>
        </div>

        {/* Multi-Persona Ensemble */}
        <div className="about-card">
          <h2>Multi-Persona Ensemble</h2>
          <p>Instead of a single LLM call, we run 3-5 diverse analyst personas, each with a different cognitive lens:</p>
          <div className="about-table">
            <div className="about-row about-row-header">
              <span>Persona</span><span>Weight</span><span>Approach</span>
            </div>
            <div className="about-row">
              <span className="about-tag blue">Deep Analyst</span><span>30%</span><span>7-step framework: parse, status check, base rates, evidence, efficiency, blind spots, calibration</span>
            </div>
            <div className="about-row">
              <span className="about-tag purple">Base Rate</span><span>25%</span><span>Pure Bayesian statistician. Reference classes + conservative updating</span>
            </div>
            <div className="about-row">
              <span className="about-tag red">Contrarian</span><span>20%</span><span>Where is the crowd wrong? Narrative bias, recency, groupthink</span>
            </div>
            <div className="about-row">
              <span className="about-tag green">Quick Gut</span><span>15%</span><span>Fast independent estimate with no anchoring to market price</span>
            </div>
            <div className="about-row">
              <span className="about-tag cyan">Momentum</span><span>10%</span><span>Price direction + volume trends + time-to-resolution signals</span>
            </div>
          </div>
          <p className="about-note">Estimates are combined in <strong>log-odds space</strong>, not linear average. This is mathematically correct for combining independent Bayesian evidence.</p>
        </div>

        {/* Log-Odds Combination */}
        <div className="about-card">
          <h2>Bayesian Log-Odds Combination</h2>
          <p>Linear averaging (e.g. 0.65 * est1 + 0.35 * est2) is wrong for probabilities. It compresses toward 0.5 and loses information. We combine in log-odds space:</p>
          <div className="about-formula">
            <div className="about-formula-label">Log-odds (logit)</div>
            <code>logit(p) = log(p / (1 - p))</code>
          </div>
          <div className="about-formula">
            <div className="about-formula-label">Combination</div>
            <code>combined = sigmoid( sum( w_i * logit(est_i) ) )</code>
          </div>
          <div className="about-formula">
            <div className="about-formula-label">Inverse (sigmoid)</div>
            <code>sigmoid(x) = 1 / (1 + e^(-x))</code>
          </div>
          <p>This naturally handles the fact that evidence at extreme probabilities (e.g. 0.95) should have much more impact than evidence near 0.5.</p>
        </div>

        {/* Extremizing */}
        <div className="about-card">
          <h2>Extremizing Transform</h2>
          <p>Research shows aggregated LLM estimates are systematically too conservative &mdash; biased toward 0.5. We correct this:</p>
          <div className="about-formula">
            <code>p_final = sigmoid( gamma * logit(p_combined) )</code>
          </div>
          <p>With <strong>gamma = 1.3</strong>, a combined estimate of 0.65 becomes 0.69. An estimate of 0.80 becomes 0.84. The further from 0.5, the stronger the push.</p>
          <div className="about-example">
            <div className="about-example-row"><span>0.55</span><span className="about-arrow" /><span>0.57</span></div>
            <div className="about-example-row"><span>0.65</span><span className="about-arrow" /><span>0.69</span></div>
            <div className="about-example-row"><span>0.80</span><span className="about-arrow" /><span>0.84</span></div>
            <div className="about-example-row"><span>0.90</span><span className="about-arrow" /><span>0.93</span></div>
          </div>
          <p className="about-note">Based on the Linear-in-Log-Odds (LLO) recalibration literature. Optimal gamma is tuned on our own resolved predictions over time.</p>
        </div>

        {/* Kelly Criterion */}
        <div className="about-card">
          <h2>Kelly Criterion (Quarter-Kelly)</h2>
          <p>For binary all-or-nothing contracts, the optimal bet fraction is:</p>
          <div className="about-formula">
            <div className="about-formula-label">Odds ratios</div>
            <code>P = market_price / (1 - market_price)</code>
            <code>Q = our_estimate / (1 - our_estimate)</code>
          </div>
          <div className="about-formula">
            <div className="about-formula-label">Kelly fraction</div>
            <code>f* = (Q - P) / (1 + Q)</code>
          </div>
          <div className="about-formula">
            <div className="about-formula-label">Final bet</div>
            <code>bet = balance * f* * confidence * 0.25</code>
          </div>
          <p>We use <strong>quarter-Kelly</strong> (0.25x) because:</p>
          <ul>
            <li>LLM probability estimates are noisy (high variance)</li>
            <li>Going from full Kelly to quarter-Kelly costs ~44% growth but reduces drawdowns dramatically</li>
            <li>Overbetting is asymmetrically worse than underbetting (quadratic penalty)</li>
          </ul>
          <p className="about-note">Source: PolySwarm (2026) uses quarter-Kelly; Thorp's asymmetry principle shows half-Kelly costs at most 25% growth rate.</p>
        </div>

        {/* YES/NO Asymmetry */}
        <div className="about-card">
          <h2>YES/NO Asymmetry (Optimism Tax)</h2>
          <p>Research on 72M+ trades shows a persistent structural edge:</p>
          <div className="about-stat-grid">
            <div className="about-stat-box">
              <div className="about-stat-number red">69 / 99</div>
              <div className="about-stat-desc">price levels where NO outperforms YES</div>
            </div>
            <div className="about-stat-box">
              <div className="about-stat-number green">+1.5%</div>
              <div className="about-stat-desc">edge bonus applied to NO positions</div>
            </div>
          </div>
          <p>People systematically overpay for YES (affirmative) outcomes &mdash; the "optimism tax". We exploit this by adding a 1.5% edge bonus to all NO-side opportunities, shifting our bot toward structurally advantaged positions.</p>
          <p className="about-note">Source: Becker (2025) microstructure analysis of Kalshi; Polymarket anatomy paper (2026)</p>
        </div>

        {/* Category Efficiency */}
        <div className="about-card">
          <h2>Category-Based Edge Thresholds</h2>
          <p>Not all markets are equally efficient. We detect the category and adjust the minimum edge required:</p>
          <div className="about-table">
            <div className="about-row about-row-header">
              <span>Category</span><span>Efficiency</span><span>Edge Multiplier</span>
            </div>
            <div className="about-row"><span>Finance</span><span>Very high (0.17pp spread)</span><span className="red">0.6x (need more edge)</span></div>
            <div className="about-row"><span>Crypto</span><span>High</span><span className="red">0.75x</span></div>
            <div className="about-row"><span>Politics</span><span>Moderate</span><span>1.0x (baseline)</span></div>
            <div className="about-row"><span>Sports</span><span>Moderate-low</span><span className="green">1.1x</span></div>
            <div className="about-row"><span>Entertainment</span><span>Low (4.79pp spread)</span><span className="green">1.3x (less edge needed)</span></div>
            <div className="about-row"><span>World Events</span><span>Lowest (7.32pp spread)</span><span className="green">1.4x</span></div>
          </div>
          <p className="about-note">For example, a 5% min_edge becomes 8.3% for finance (5% / 0.6) but only 3.6% for world events (5% / 1.4).</p>
        </div>

        {/* Calibration */}
        <div className="about-card">
          <h2>Calibration &amp; Self-Improvement</h2>
          <p>Every prediction is logged with its outcome. We track accuracy using two scoring rules:</p>
          <div className="about-formula">
            <div className="about-formula-label">Brier Score (lower = better)</div>
            <code>BS = (1/N) * sum( (forecast - outcome)^2 )</code>
          </div>
          <div className="about-formula">
            <div className="about-formula-label">Log Loss (penalizes confident mistakes)</div>
            <code>LL = -(1/N) * sum( o*log(f) + (1-o)*log(1-f) )</code>
          </div>
          <p><strong>Brier Decomposition</strong> tells us <em>why</em> we're wrong:</p>
          <ul>
            <li><strong>Reliability</strong> (calibration error) &mdash; are our 70% predictions winning 70% of the time?</li>
            <li><strong>Resolution</strong> &mdash; do we differentiate enough between events?</li>
            <li><strong>Uncertainty</strong> &mdash; inherent unpredictability (fixed for a given dataset)</li>
          </ul>
          <p>After 30+ resolved predictions, we auto-fit <strong>Platt scaling</strong>:</p>
          <div className="about-formula">
            <code>p_calibrated = sigmoid( a * logit(p_raw) + b )</code>
          </div>
          <p className="about-note">Parameters a and b are fit via gradient descent on resolved predictions. This corrects both slope (over/under-confidence) and bias simultaneously.</p>
        </div>

        {/* NegRisk Arbitrage */}
        <div className="about-card">
          <h2>NegRisk Arbitrage</h2>
          <p>Multi-outcome markets (e.g. "Who will win the election?" with 5 candidates) should have YES prices summing to 1.0. When they don't, it's free money:</p>
          <div className="about-stat-grid">
            <div className="about-stat-box">
              <div className="about-stat-number green">$29M+</div>
              <div className="about-stat-desc">extracted from NegRisk mispricing (Apr 2024 - Apr 2025)</div>
            </div>
            <div className="about-stat-box">
              <div className="about-stat-number blue">29x</div>
              <div className="about-stat-desc">capital efficiency vs binary arbitrage</div>
            </div>
          </div>
          <ul>
            <li>If sum {'>'} 1.0: buy all NO contracts (one must pay out, profit = sum - 1.0)</li>
            <li>If sum {'<'} 1.0: buy all YES contracts (one must pay out, profit = 1.0 - sum)</li>
          </ul>
          <p className="about-note">Source: "Unravelling the Probabilistic Forest" (2025)</p>
        </div>

        {/* Cross-Market Correlation */}
        <div className="about-card">
          <h2>Cross-Market Correlation</h2>
          <p>Related markets should have consistent probability estimates. We detect correlated markets via keyword overlap and flag when our estimates diverge by more than 30%.</p>
          <p>The Semantic Trading paper (2025) achieved <strong>24.8-47.5% monthly ROI</strong> by trading leader-follower relationships between correlated prediction markets.</p>
          <p className="about-note">Current implementation uses keyword overlap; future versions could use embedding-based semantic similarity.</p>
        </div>

        {/* Confidence */}
        <div className="about-card">
          <h2>Confidence Scoring</h2>
          <p>Confidence is derived from <strong>ensemble agreement</strong> (standard deviation of persona estimates) and <strong>volume adjustment</strong>:</p>
          <div className="about-table">
            <div className="about-row about-row-header">
              <span>Std Dev</span><span>Confidence</span>
            </div>
            <div className="about-row"><span>{'<'} 3%</span><span className="green">95% (strong agreement)</span></div>
            <div className="about-row"><span>{'<'} 6%</span><span className="green">85%</span></div>
            <div className="about-row"><span>{'<'} 10%</span><span>70%</span></div>
            <div className="about-row"><span>{'<'} 15%</span><span className="red">55%</span></div>
            <div className="about-row"><span>{'>'} 15%</span><span className="red">40% (major disagreement)</span></div>
          </div>
          <p>Then scaled by volume (high-volume markets are more efficient):</p>
          <ul>
            <li>{'>'} $2M volume: confidence * 0.6</li>
            <li>{'>'} $1M volume: confidence * 0.7</li>
            <li>{'>'} $500K volume: confidence * 0.85</li>
          </ul>
        </div>

        {/* Research Sources */}
        <div className="about-card about-wide">
          <h2>Research Sources</h2>
          <div className="about-sources">
            <div className="about-source">
              <span className="about-source-title">PolySwarm (2026)</span>
              <span className="about-source-desc">50-agent LLM swarm with confidence-weighted Bayesian aggregation and quarter-Kelly</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">Prediction Arena (2026)</span>
              <span className="about-source-desc">Benchmark of frontier models trading live on Kalshi/Polymarket with real money</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">Semantic Trading (2025)</span>
              <span className="about-source-desc">LLM-based clustering for correlated pairs. 24.8-47.5% monthly ROI</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">Anatomy of Polymarket (2026)</span>
              <span className="about-source-desc">Comprehensive microstructure analysis. Kyle's lambda declined 50x as market matured</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">Kelly for Prediction Markets (2024)</span>
              <span className="about-source-desc">Derives optimal Kelly fraction for all-or-nothing contracts</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">Microstructure of Wealth Transfer (2025)</span>
              <span className="about-source-desc">72.1M trades showing systematic taker losses and YES/NO asymmetry</span>
            </div>
            <div className="about-source">
              <span className="about-source-title">ForecastBench (ongoing)</span>
              <span className="about-source-desc">LLM calibration benchmark. Best LLM Brier 0.101 vs superforecasters 0.081</span>
            </div>
          </div>
        </div>

      </div>
    </div>
  )
}

// ── App ──────────────────────────────────────────

export default function App() {
  const [stats, setStats] = useState(null)
  const [botStatus, setBotStatus] = useState({ running: false, paused: false })
  const [refreshKey, setRefreshKey] = useState(0)
  const hash = useHash()

  // Stable callbacks for WS hook
  const handleStats = useCallback((s) => {
    setStats(s)
    setRefreshKey(k => k + 1)
  }, [])
  const handleBotStatus = useCallback((s) => setBotStatus(s), [])

  const events = useWebSocket(handleStats, handleBotStatus)

  // Fetch initial state via REST (don't rely solely on WS)
  useEffect(() => {
    apiGet('/api/state').then(d => { if (d) setStats(d) })
    apiGet('/api/bot/status').then(d => { if (d) setBotStatus(d) })
  }, [])

  const handleBotAction = async (action) => {
    const result = await apiPost(`/api/bot/${action}`)
    if (result) {
      // Immediately update from response
      const s = result.status
      if (s === 'started' || s === 'resumed' || s === 'already_running') {
        setBotStatus({ running: true, paused: false })
      } else if (s === 'paused') {
        setBotStatus({ running: true, paused: true })
      } else if (s === 'stopped') {
        setBotStatus({ running: false, paused: false })
      }
    }
    // Always re-fetch real status as fallback
    const fresh = await apiGet('/api/bot/status')
    if (fresh) setBotStatus(fresh)
  }

  const handleReset = async () => {
    const result = await apiPost('/api/reset')
    if (result) {
      const s = await apiGet('/api/state')
      if (s) setStats(s)
      setRefreshKey(k => k + 1)
    }
  }

  if (hash === '#about') return <AboutPage />

  return (
    <>
      <Header stats={stats} botStatus={botStatus} onBotAction={handleBotAction} />
      <DeployBar stats={stats} />
      <div className="grid">
        <LiveFeed events={events} />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <Portfolio refreshKey={refreshKey} />
          <Opportunities onTraded={() => setRefreshKey(k => k + 1)} />
        </div>
      </div>
      <div className="grid grid-3">
        <History refreshKey={refreshKey} />
        <Calibration />
        <Settings />
      </div>
      <ChartsSection refreshKey={refreshKey} />
      <div className="grid grid-3">
        <div className="card">
          <div className="card-title">Quick Actions</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 0' }}>
            <button className="btn" onClick={() => handleBotAction(botStatus.running ? (botStatus.paused ? 'start' : 'pause') : 'start')}>
              {botStatus.running ? (botStatus.paused ? 'Resume Bot' : 'Pause Bot') : 'Start Autopilot'}
            </button>
            {botStatus.running && (
              <button className="btn btn-red" onClick={() => handleBotAction('stop')}>Stop Bot</button>
            )}
            <button className="btn" style={{ marginTop: 8 }} onClick={handleReset}>
              Reset Paper Account
            </button>
          </div>
          {stats && (
            <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text2)', fontFamily: 'var(--mono)' }}>
              <div>Balance: ${stats.balance?.toFixed(2)}</div>
              <div>Trades: {stats.total_trades}</div>
              <div>Record: {stats.wins}W / {stats.losses}L</div>
              <div>P&L: {fmtPnl(stats.total_pnl)} ({stats.total_pnl_pct?.toFixed(1)}%)</div>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
