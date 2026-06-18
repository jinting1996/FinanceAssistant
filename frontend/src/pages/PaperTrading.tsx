import { useEffect, useState, useCallback } from 'react'
import { RefreshCw, Power, RotateCcw, X, TrendingUp, TrendingDown, Trophy, BarChart3, Wallet, Activity, Play, Bell, SlidersHorizontal, ListChecks } from 'lucide-react'
import {
  paperTradingApi,
  type PaperTradingAccountResponse,
  type PaperTradingPositionItem,
  type PaperTradingTradeItem,
  type EquityCurvePoint,
  type StrategyPerformanceItem,
  type NotifyChannelItem,
  type MarketView,
  type PaperTradingStrategySelection,
  type PaperTradingStrategySelectionResponse,
  type PaperTradingMetricsResponse,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

const EXIT_REASON_MAP: Record<string, string> = {
  stop_loss: '止损',
  target_price: '止盈',
  signal_reversal: '信号反转',
  manual: '手动平仓',
}

const SKIP_REASON_LABELS: Record<string, string> = {
  strategy_not_selected: '策略未选中',
  existing_position: '已有持仓',
  duplicate_signal: '信号已处理',
  no_quote: '缺行情',
  invalid_price: '价格无效',
  below_entry_range: '低于入场区间',
  above_entry_range: '高于入场区间',
  market_disabled: '市场未分配资金',
  quantity_too_small: '仓位不足一手',
  insufficient_cash: '资金不足',
  signal_expired: '信号过期',
}

function formatCurrency(v: number) {
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function PnlText({ value, suffix = '' }: { value: number; suffix?: string }) {
  const color = value > 0 ? 'text-rose-500' : value < 0 ? 'text-emerald-500' : 'text-muted-foreground'
  const prefix = value > 0 ? '+' : ''
  return <span className={color}>{prefix}{formatCurrency(value)}{suffix}</span>
}

function PnlPctText({ value }: { value: number }) {
  const color = value > 0 ? 'text-rose-500' : value < 0 ? 'text-emerald-500' : 'text-muted-foreground'
  const prefix = value > 0 ? '+' : ''
  return <span className={color}>{prefix}{value.toFixed(2)}%</span>
}

function ExitReasonSummary({ counts }: { counts?: Record<string, number> }) {
  const entries = Object.entries(counts || {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 2)
  if (entries.length === 0) return <span className="text-muted-foreground">-</span>
  return (
    <div className="flex flex-wrap justify-end gap-1">
      {entries.map(([reason, count]) => (
        <span key={reason} className="text-[11px] px-1.5 py-0.5 rounded bg-accent text-muted-foreground">
          {EXIT_REASON_MAP[reason] || reason} {count}
        </span>
      ))}
    </div>
  )
}

function EquityChart({ data, baseline, mode }: { data: EquityCurvePoint[]; baseline: number; mode: 'pct' | 'value' }) {
  if (data.length < 2) {
    return <div className="h-32 flex items-center justify-center text-muted-foreground text-sm">暂无足够数据绘制曲线</div>
  }

  const width = 600
  const height = 130
  const pad = { top: 14, right: 16, bottom: 22, left: 52 }
  const w = width - pad.left - pad.right
  const h = height - pad.top - pad.bottom

  const values = data.map(d => d.equity)
  // 纵轴锚定基准(账户:初始资金;策略:0),并设最小跨度,避免微小波动被拉满高度
  let lo = Math.min(...values, baseline)
  let hi = Math.max(...values, baseline)
  const minSpan = Math.abs(baseline) * 0.05
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2
    lo = mid - minSpan / 2
    hi = mid + minSpan / 2
  }
  const padV = (hi - lo) * 0.12 || 1
  lo -= padV
  hi += padV
  const range = hi - lo || 1
  const yOf = (v: number) => pad.top + h - ((v - lo) / range) * h

  const points = data.map((d, i) => ({ x: pad.left + (i / (data.length - 1)) * w, y: yOf(d.equity), ...d }))
  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ')
  const areaD = pathD + ` L${points[points.length - 1].x},${pad.top + h} L${points[0].x},${pad.top + h} Z`

  const isPositive = values[values.length - 1] >= baseline
  const strokeColor = isPositive ? '#f43f5e' : '#10b981'
  const fillColor = isPositive ? 'rgba(244,63,94,0.1)' : 'rgba(16,185,129,0.1)'

  const fmt = (v: number) => {
    if (mode === 'pct') return baseline ? `${(((v / baseline) - 1) * 100).toFixed(1)}%` : '0%'
    return Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(2)}万` : `${v.toFixed(0)}`
  }

  const yTicks = 3
  const yLabels = Array.from({ length: yTicks + 1 }, (_, i) => {
    const v = lo + (range / yTicks) * i
    return { v, y: pad.top + h - (i / yTicks) * h }
  })
  const baselineY = yOf(baseline)
  const xIndices = [0, Math.floor(data.length / 2), data.length - 1]
  const xLabels = xIndices.map(i => ({ label: data[i].date.slice(5), x: points[i].x }))

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" preserveAspectRatio="xMidYMid meet">
      {yLabels.map((t, i) => (
        <g key={i}>
          <line x1={pad.left} x2={width - pad.right} y1={t.y} y2={t.y} stroke="hsl(var(--border))" strokeWidth={0.5} />
          <text x={pad.left - 6} y={t.y + 4} textAnchor="end" fill="hsl(var(--muted-foreground))" fontSize={10}>
            {fmt(t.v)}
          </text>
        </g>
      ))}
      {/* 基准线 */}
      <line x1={pad.left} x2={width - pad.right} y1={baselineY} y2={baselineY} stroke="hsl(var(--muted-foreground))" strokeWidth={0.8} strokeDasharray="3 3" opacity={0.5} />
      <path d={areaD} fill={fillColor} />
      <path d={pathD} fill="none" stroke={strokeColor} strokeWidth={2} />
      {xLabels.map((l, i) => (
        <text key={i} x={l.x} y={height - 6} textAnchor="middle" fill="hsl(var(--muted-foreground))" fontSize={10}>
          {l.label}
        </text>
      ))}
    </svg>
  )
}

export default function PaperTradingPage() {
  const { toast } = useToast()
  const [account, setAccount] = useState<PaperTradingAccountResponse | null>(null)
  const [positions, setPositions] = useState<PaperTradingPositionItem[]>([])
  const [trades, setTrades] = useState<PaperTradingTradeItem[]>([])
  const [tradesTotal, setTradesTotal] = useState(0)
  const [equityCurve, setEquityCurve] = useState<EquityCurvePoint[]>([])
  const [curveStrategy, setCurveStrategy] = useState('')
  const [strategyCurve, setStrategyCurve] = useState<EquityCurvePoint[]>([])
  const [strategyPerf, setStrategyPerf] = useState<StrategyPerformanceItem[]>([])
  const [skipStats, setSkipStats] = useState<PaperTradingMetricsResponse['skip_stats'] | null>(null)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [tradesPage, setTradesPage] = useState(0)
  const tradesPageSize = 20

  // 市场视图（分段单选，切换即按该市场口径刷新统计）
  const [marketView, setMarketView] = useState<MarketView>('ALL')

  // 资金配置
  const [configOpen, setConfigOpen] = useState(false)
  const [cfgTotal, setCfgTotal] = useState('')
  const [cfgRatios, setCfgRatios] = useState<{ CN: string; HK: string; US: string }>({ CN: '', HK: '', US: '' })
  const [cfgSaving, setCfgSaving] = useState(false)

  // 通知设置
  const [tradesOpen, setTradesOpen] = useState(false)
  const [notifyOpen, setNotifyOpen] = useState(false)
  const [notifyEnabled, setNotifyEnabled] = useState(false)
  const [notifyRealtime, setNotifyRealtime] = useState(true)
  const [notifyPremarket, setNotifyPremarket] = useState(true)
  const [notifySummary, setNotifySummary] = useState(true)
  const [notifyChannels, setNotifyChannels] = useState<NotifyChannelItem[]>([])
  const [selectedChannelIds, setSelectedChannelIds] = useState<Set<number>>(new Set())
  const [notifySaving, setNotifySaving] = useState(false)
  const [notifyTesting, setNotifyTesting] = useState(false)

  // 策略选择
  const [strategyOpen, setStrategyOpen] = useState(false)
  const [strategySelection, setStrategySelection] = useState<PaperTradingStrategySelection>({ mode: 'all', strategy_codes: [], top_n: 5 })
  const [strategyPool, setStrategyPool] = useState<PaperTradingStrategySelectionResponse['strategy_pool']>([])
  const [strategySaving, setStrategySaving] = useState(false)

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const mkt = marketView === 'ALL' ? undefined : marketView
      const [acc, pos, tradeData, metrics] = await Promise.all([
        paperTradingApi.getAccount(mkt),
        paperTradingApi.listPositions('open', mkt),
        paperTradingApi.listTrades(tradesPageSize, tradesPage * tradesPageSize, mkt),
        paperTradingApi.getMetrics(mkt),
      ])
      setAccount(acc)
      setPositions(pos)
      setTrades(tradeData.items)
      setTradesTotal(tradeData.total)
      setEquityCurve(metrics.equity_curve)
      setStrategyPerf(metrics.strategy_performance || [])
      setSkipStats(metrics.skip_stats || null)
    } catch {
      toast('加载失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [tradesPage, marketView])

  useEffect(() => { loadData() }, [loadData])

  // 按策略收益曲线(累计盈亏),仅在选中具体策略时按需拉取
  useEffect(() => {
    if (!curveStrategy) { setStrategyCurve([]); return }
    let cancelled = false
    const mkt = marketView === 'ALL' ? undefined : marketView
    paperTradingApi.getMetrics(mkt, curveStrategy)
      .then(m => { if (!cancelled) setStrategyCurve(m.equity_curve || []) })
      .catch(() => { if (!cancelled) setStrategyCurve([]) })
    return () => { cancelled = true }
  }, [curveStrategy, marketView])

  const handleToggle = async () => {
    if (!account) return
    try {
      const res = await paperTradingApi.toggleAccount(!account.enabled)
      setAccount(res)
      toast(res.enabled ? '模拟盘已启动' : '模拟盘已暂停', 'success')
    } catch {
      toast('操作失败', 'error')
    }
  }

  const handleReset = async () => {
    if (!confirm('确定重置模拟盘？所有持仓和交易记录将被清空。')) return
    try {
      await paperTradingApi.resetAccount()
      toast('模拟盘已重置', 'success')
      loadData()
    } catch {
      toast('重置失败', 'error')
    }
  }

  const handleScan = async () => {
    setScanning(true)
    try {
      const res = await paperTradingApi.scan()
      toast(`扫描完成: 建仓 ${res.opened ?? 0} 笔, 平仓 ${res.closed ?? 0} 笔`, 'success')
      loadData()
    } catch {
      toast('扫描失败', 'error')
    } finally {
      setScanning(false)
    }
  }

  const handleClosePosition = async (id: number) => {
    try {
      await paperTradingApi.closePosition(id)
      toast('平仓成功', 'success')
      loadData()
    } catch {
      toast('平仓失败', 'error')
    }
  }

  const handleOpenConfig = async () => {
    setConfigOpen(true)
    try {
      // 以"全部"口径取总资金与各市场比例
      const acc = await paperTradingApi.getAccount()
      setCfgTotal(String(Math.round(acc.initial_capital)))
      const a = acc.market_allocations || {}
      setCfgRatios({
        CN: String(Math.round((a.CN ?? 0) * 100)),
        HK: String(Math.round((a.HK ?? 0) * 100)),
        US: String(Math.round((a.US ?? 0) * 100)),
      })
    } catch {
      toast('加载配置失败', 'error')
    }
  }

  const handleSaveConfig = async () => {
    const total = Number(cfgTotal)
    const cn = Number(cfgRatios.CN) || 0
    const hk = Number(cfgRatios.HK) || 0
    const us = Number(cfgRatios.US) || 0
    if (!(total > 0)) {
      toast('总资金需大于 0', 'error')
      return
    }
    if (cn + hk + us > 100) {
      toast('比例合计不能超过 100%', 'error')
      return
    }
    setCfgSaving(true)
    try {
      await paperTradingApi.updateSettings({
        initial_capital: total,
        market_allocations: { CN: cn / 100, HK: hk / 100, US: us / 100 },
      })
      toast('资金配置已保存', 'success')
      setConfigOpen(false)
      loadData()
    } catch {
      toast('保存失败', 'error')
    } finally {
      setCfgSaving(false)
    }
  }

  const loadNotifySettings = async () => {
    try {
      const data = await paperTradingApi.getNotifySettings()
      const s = data.settings
      setNotifyEnabled(s.pt_notify_enabled === 'true')
      setNotifyRealtime(s.pt_notify_realtime === 'true')
      setNotifyPremarket(s.pt_notify_premarket === 'true')
      setNotifySummary(s.pt_notify_summary === 'true')
      setNotifyChannels(data.channels)
      const ids = s.pt_notify_channel_ids
        ? new Set(s.pt_notify_channel_ids.split(',').map(Number).filter(Boolean))
        : new Set<number>()
      setSelectedChannelIds(ids)
    } catch {
      toast('加载通知配置失败', 'error')
    }
  }

  const handleOpenNotify = async () => {
    setNotifyOpen(true)
    await loadNotifySettings()
  }

  const handleOpenStrategySelection = async () => {
    setStrategyOpen(true)
    try {
      const data = await paperTradingApi.getStrategySelection()
      setStrategySelection(data.selection)
      setStrategyPool(data.strategy_pool || [])
    } catch {
      toast('加载策略选择失败', 'error')
    }
  }

  const handleSaveStrategySelection = async () => {
    setStrategySaving(true)
    try {
      const data = await paperTradingApi.updateStrategySelection(strategySelection)
      setStrategySelection(data.selection)
      setStrategyPool(data.strategy_pool || [])
      setStrategyOpen(false)
      toast('策略选择已保存', 'success')
    } catch {
      toast('保存策略选择失败', 'error')
    } finally {
      setStrategySaving(false)
    }
  }

  const toggleStrategyCode = (code: string) => {
    setStrategySelection(prev => {
      const set = new Set(prev.strategy_codes || [])
      if (set.has(code)) set.delete(code)
      else set.add(code)
      return { ...prev, strategy_codes: Array.from(set) }
    })
  }

  const handleSaveNotify = async () => {
    setNotifySaving(true)
    try {
      await paperTradingApi.updateNotifySettings({
        pt_notify_enabled: notifyEnabled ? 'true' : 'false',
        pt_notify_channel_ids: Array.from(selectedChannelIds).join(','),
        pt_notify_realtime: notifyRealtime ? 'true' : 'false',
        pt_notify_premarket: notifyPremarket ? 'true' : 'false',
        pt_notify_summary: notifySummary ? 'true' : 'false',
      })
      toast('通知配置已保存', 'success')
      setNotifyOpen(false)
    } catch {
      toast('保存失败', 'error')
    } finally {
      setNotifySaving(false)
    }
  }

  const handleTestNotify = async () => {
    setNotifyTesting(true)
    try {
      await paperTradingApi.testNotify()
      toast('测试通知已发送', 'success')
    } catch {
      toast('测试通知发送失败', 'error')
    } finally {
      setNotifyTesting(false)
    }
  }

  const toggleChannel = (id: number) => {
    setSelectedChannelIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const totalPages = Math.ceil(tradesTotal / tradesPageSize)
  const ratioSum = (Number(cfgRatios.CN) || 0) + (Number(cfgRatios.HK) || 0) + (Number(cfgRatios.US) || 0)

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shrink-0">
            <Activity className="w-4 h-4 text-white" />
          </div>
          <h1 className="text-lg font-bold">模拟盘</h1>
          {account && (
            <span className={`text-xs px-2 py-0.5 rounded-full ${account.enabled ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'}`}>
              {account.enabled ? '运行中' : '已暂停'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {tradesTotal > 0 && (
            <Button variant="outline" size="sm" className="h-8" onClick={() => setTradesOpen(true)}>
              <BarChart3 className="w-3.5 h-3.5" />
              <span className="hidden sm:inline ml-1">平仓记录 ({tradesTotal})</span>
              <span className="sm:hidden ml-1">{tradesTotal}</span>
            </Button>
          )}
          <Button variant="outline" size="sm" className="h-8" onClick={handleOpenNotify}>
            <Bell className="w-3.5 h-3.5" />
            <span className="hidden sm:inline ml-1">通知</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={handleOpenStrategySelection}>
            <ListChecks className="w-3.5 h-3.5" />
            <span className="hidden sm:inline ml-1">策略</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={handleScan} disabled={scanning}>
            <Play className="w-3.5 h-3.5 mr-1" />
            <span className="hidden sm:inline">{scanning ? '扫描中...' : '立即扫描'}</span>
            <span className="sm:hidden">{scanning ? '扫描中' : '扫描'}</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={loadData} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span className="hidden sm:inline ml-1">刷新</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={handleToggle}>
            <Power className="w-3.5 h-3.5" />
            <span className="hidden sm:inline ml-1">{account?.enabled ? '暂停' : '启动'}</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8 text-destructive hover:text-destructive" onClick={handleReset}>
            <RotateCcw className="w-3.5 h-3.5" />
            <span className="hidden sm:inline ml-1">重置</span>
          </Button>
        </div>
      </div>

      {/* Market View Filter + 资金配置 */}
      {account && (
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground text-xs">交易市场:</span>
            {(['ALL', 'CN', 'HK', 'US'] as const).map(m => {
              const label = m === 'ALL' ? '全部' : m === 'CN' ? 'A股' : m === 'HK' ? '港股' : '美股'
              const active = marketView === m
              const ratio = m !== 'ALL' ? account.market_allocations?.[m] : undefined
              const isOff = m !== 'ALL' && (ratio ?? 0) <= 0
              return (
                <button
                  key={m}
                  onClick={() => setMarketView(m)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                    active
                      ? 'bg-primary text-primary-foreground'
                      : isOff
                      ? 'bg-muted/50 text-muted-foreground'
                      : 'bg-primary/10 text-primary ring-1 ring-primary/20'
                  }`}
                >
                  {label}{m !== 'ALL' && ratio != null ? ` ${Math.round(ratio * 100)}%` : ''}
                </button>
              )
            })}
          </div>
          <Button variant="outline" size="sm" className="h-8" onClick={handleOpenConfig}>
            <SlidersHorizontal className="w-3.5 h-3.5" />
            <span className="hidden sm:inline ml-1">资金配置</span>
          </Button>
        </div>
      )}

      {/* Summary Cards */}
      {account && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div className="card p-3">
            <div className="flex items-center gap-1.5 text-muted-foreground text-xs mb-1">
              <Wallet className="w-3.5 h-3.5" />
              总资产
            </div>
            <div className="text-lg font-bold">{formatCurrency(account.total_equity)}</div>
          </div>
          <div className="card p-3">
            <div className="flex items-center gap-1.5 text-muted-foreground text-xs mb-1">
              {account.total_pnl >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
              总收益
            </div>
            <div className="text-lg font-bold"><PnlText value={account.total_pnl} /></div>
          </div>
          <div className="card p-3">
            <div className="flex items-center gap-1.5 text-muted-foreground text-xs mb-1">
              <Trophy className="w-3.5 h-3.5" />
              胜率
            </div>
            <div className="text-lg font-bold">{account.win_rate.toFixed(1)}%</div>
            <div className="text-xs text-muted-foreground">{account.winning_trades}/{account.total_trades} 笔</div>
          </div>
          <div className="card p-3">
            <div className="flex items-center gap-1.5 text-muted-foreground text-xs mb-1">
              <BarChart3 className="w-3.5 h-3.5" />
              最大回撤
            </div>
            <div className="text-lg font-bold text-emerald-500">{account.max_drawdown_pct.toFixed(2)}%</div>
          </div>
          <div className="card p-3">
            <div className="flex items-center gap-1.5 text-muted-foreground text-xs mb-1">
              <Wallet className="w-3.5 h-3.5" />
              可用资金
            </div>
            <div className="text-lg font-bold">{formatCurrency(account.current_capital)}</div>
          </div>
        </div>
      )}

      {/* Equity Curve */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-3 gap-2">
          <h2 className="text-sm font-semibold">{curveStrategy ? '策略累计盈亏' : '收益曲线'}</h2>
          <select
            className="h-8 px-2 rounded-lg border border-border bg-background text-xs max-w-[180px]"
            value={curveStrategy}
            onChange={e => setCurveStrategy(e.target.value)}
          >
            <option value="">全部(账户净值)</option>
            {strategyPerf.map(s => (
              <option key={s.strategy_code} value={s.strategy_code}>{s.strategy_code}</option>
            ))}
          </select>
        </div>
        {curveStrategy
          ? <EquityChart data={strategyCurve} baseline={0} mode="value" />
          : <EquityChart data={equityCurve} baseline={equityCurve[0]?.equity ?? 0} mode="pct" />}
      </div>

      {/* Strategy Performance */}
      {strategyPerf.length > 0 && (
        <div className="card p-4">
          <h2 className="text-sm font-semibold mb-3">策略绩效</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-3">策略</th>
                  <th className="text-right py-2 px-2">已平仓</th>
                  <th className="text-right py-2 px-2">胜率</th>
                  <th className="text-right py-2 px-2">已实现盈亏</th>
                  <th className="text-right py-2 px-2">平均盈亏%</th>
                  <th className="text-right py-2 px-2">平均持仓天数</th>
                  <th className="text-right py-2 px-2">持仓中</th>
                  <th className="text-right py-2 px-2">跳过</th>
                  <th className="text-right py-2 px-2">退出原因</th>
                  <th className="text-right py-2 pl-2">浮动盈亏</th>
                </tr>
              </thead>
              <tbody>
                {strategyPerf.map(s => (
                  <tr key={s.strategy_code} className="border-b border-border/50 hover:bg-accent/30">
                    <td className="py-2 pr-3 font-medium">{s.strategy_code}</td>
                    <td className="text-right py-2 px-2">{s.total_trades}</td>
                    <td className="text-right py-2 px-2">
                      {s.total_trades > 0 ? (
                        <span className={s.win_rate >= 50 ? 'text-rose-500' : s.win_rate > 0 ? 'text-amber-500' : 'text-muted-foreground'}>
                          {s.win_rate.toFixed(1)}%
                        </span>
                      ) : '-'}
                    </td>
                    <td className="text-right py-2 px-2"><PnlText value={s.total_pnl} /></td>
                    <td className="text-right py-2 px-2"><PnlPctText value={s.avg_pnl_pct} /></td>
                    <td className="text-right py-2 px-2">{s.total_trades > 0 ? `${s.avg_holding_days}天` : '-'}</td>
                    <td className="text-right py-2 px-2">{s.open_positions > 0 ? s.open_positions : '-'}</td>
                    <td className="text-right py-2 px-2">{s.skipped_count ? s.skipped_count : '-'}</td>
                    <td className="text-right py-2 px-2"><ExitReasonSummary counts={s.exit_reason_counts} /></td>
                    <td className="text-right py-2 pl-2">
                      {s.open_positions > 0 ? <PnlText value={s.unrealized_pnl} /> : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {(skipStats?.total ?? 0) > 0 && (
        <div className="card p-4">
          <h2 className="text-sm font-semibold mb-3">最近扫描跳过原因</h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(skipStats?.by_reason || {}).map(([reason, count]) => (
              <span key={reason} className="text-xs px-2.5 py-1 rounded-full bg-accent text-muted-foreground">
                {SKIP_REASON_LABELS[reason] || reason}: {String(count)}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Open Positions */}
      <div className="card p-4">
        <h2 className="text-sm font-semibold mb-3">当前持仓 ({positions.length})</h2>
        {positions.length === 0 ? (
          <div className="text-center text-muted-foreground text-sm py-8">暂无持仓</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-3">股票</th>
                  <th className="text-right py-2 px-2">入场价</th>
                  <th className="text-right py-2 px-2">现价</th>
                  <th className="text-right py-2 px-2">浮动盈亏</th>
                  <th className="text-right py-2 px-2">止损</th>
                  <th className="text-right py-2 px-2">止盈</th>
                  <th className="text-left py-2 px-2">策略</th>
                  <th className="text-right py-2 px-2">持仓天数</th>
                  <th className="text-right py-2 pl-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {positions.map(p => (
                  <tr key={p.id} className="border-b border-border/50 hover:bg-accent/30">
                    <td className="py-2 pr-3">
                      <div className="font-medium">{p.stock_name || p.stock_symbol}</div>
                      <div className="text-xs text-muted-foreground">{p.stock_symbol} · {p.stock_market}</div>
                    </td>
                    <td className="text-right py-2 px-2">{p.entry_price.toFixed(2)}</td>
                    <td className="text-right py-2 px-2">{p.current_price?.toFixed(2) ?? '-'}</td>
                    <td className="text-right py-2 px-2">
                      <PnlText value={p.unrealized_pnl} />
                      <div className="text-xs"><PnlPctText value={p.unrealized_pnl_pct} /></div>
                    </td>
                    <td className="text-right py-2 px-2">{p.stop_loss?.toFixed(2) ?? '-'}</td>
                    <td className="text-right py-2 px-2">{p.target_price?.toFixed(2) ?? '-'}</td>
                    <td className="py-2 px-2 text-xs text-muted-foreground">{p.strategy_code || '-'}</td>
                    <td className="text-right py-2 px-2">{p.holding_days}天</td>
                    <td className="text-right py-2 pl-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-destructive hover:text-destructive"
                        onClick={() => handleClosePosition(p.id)}
                      >
                        <X className="w-3.5 h-3.5 mr-0.5" />
                        平仓
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Trade History Dialog */}
      <Dialog open={tradesOpen} onOpenChange={setTradesOpen}>
        <DialogContent className="max-w-4xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>已平仓记录 ({tradesTotal})</DialogTitle>
            <DialogDescription>历史交易详情</DialogDescription>
          </DialogHeader>
          {trades.length === 0 ? (
            <div className="text-center text-muted-foreground text-sm py-8">暂无交易记录</div>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground text-xs">
                      <th className="text-left py-2 pr-3">股票</th>
                      <th className="text-right py-2 px-2">入场价</th>
                      <th className="text-right py-2 px-2">出场价</th>
                      <th className="text-right py-2 px-2">盈亏</th>
                      <th className="text-right py-2 px-2">盈亏%</th>
                      <th className="text-left py-2 px-2">出场原因</th>
                      <th className="text-left py-2 px-2">策略</th>
                      <th className="text-right py-2 px-2">持仓天数</th>
                      <th className="text-right py-2 pl-2">平仓时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map(t => (
                      <tr key={t.id} className="border-b border-border/50 hover:bg-accent/30">
                        <td className="py-2 pr-3">
                          <div className="font-medium">{t.stock_name || t.stock_symbol}</div>
                          <div className="text-xs text-muted-foreground">{t.stock_symbol} · {t.stock_market}</div>
                        </td>
                        <td className="text-right py-2 px-2">{t.entry_price.toFixed(2)}</td>
                        <td className="text-right py-2 px-2">{t.exit_price.toFixed(2)}</td>
                        <td className="text-right py-2 px-2"><PnlText value={t.pnl} /></td>
                        <td className="text-right py-2 px-2"><PnlPctText value={t.pnl_pct} /></td>
                        <td className="py-2 px-2 text-xs">{EXIT_REASON_MAP[t.exit_reason] || t.exit_reason}</td>
                        <td className="py-2 px-2 text-xs text-muted-foreground">{t.strategy_code || '-'}</td>
                        <td className="text-right py-2 px-2">{t.holding_days}天</td>
                        <td className="text-right py-2 pl-2 text-xs text-muted-foreground">{t.closed_at?.slice(0, 10) || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {totalPages > 1 && (
                <div className="flex items-center justify-center gap-2 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={tradesPage === 0}
                    onClick={() => setTradesPage(p => Math.max(0, p - 1))}
                  >
                    上一页
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    {tradesPage + 1} / {totalPages}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={tradesPage >= totalPages - 1}
                    onClick={() => setTradesPage(p => p + 1)}
                  >
                    下一页
                  </Button>
                </div>
              )}
            </>
          )}
        </DialogContent>
      </Dialog>

      {/* 资金配置对话框 */}
      <Dialog open={configOpen} onOpenChange={setConfigOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>资金配置</DialogTitle>
            <DialogDescription>设置总资金与各市场投资比例，比例为 0 则不投入该市场（已有持仓不受影响，仅停止新建仓）</DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div>
              <div className="text-sm font-medium mb-1">总资金</div>
              <input
                type="number"
                value={cfgTotal}
                onChange={e => setCfgTotal(e.target.value)}
                className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm"
                placeholder="如 1000000"
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm font-medium">
                <span>各市场投资比例</span>
                <span className={`text-xs ${ratioSum > 100 ? 'text-destructive' : 'text-muted-foreground'}`}>
                  合计 {ratioSum}%{ratioSum > 100 ? '（超过 100%）' : ''}
                </span>
              </div>
              {(['CN', 'HK', 'US'] as const).map(m => {
                const label = m === 'CN' ? 'A股' : m === 'HK' ? '港股' : '美股'
                const pct = Number(cfgRatios[m]) || 0
                const amount = ((Number(cfgTotal) || 0) * pct) / 100
                return (
                  <div key={m} className="flex items-center gap-3">
                    <span className="w-12 text-sm">{label}</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={cfgRatios[m]}
                      onChange={e => setCfgRatios(prev => ({ ...prev, [m]: e.target.value }))}
                      className="w-20 h-9 px-2 rounded-lg border border-border bg-background text-sm text-right"
                    />
                    <span className="text-sm text-muted-foreground">%</span>
                    <span className="text-xs text-muted-foreground ml-auto">≈ {formatCurrency(amount)}</span>
                  </div>
                )
              })}
              <div className="text-xs text-muted-foreground">合计可小于 100%，余下为闲置不投入的资金。</div>
            </div>

            <div className="flex items-center gap-2 pt-1">
              <Button size="sm" onClick={handleSaveConfig} disabled={cfgSaving || ratioSum > 100}>
                {cfgSaving ? '保存中...' : '保存'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* 跟单通知设置对话框 */}
      <Dialog open={strategyOpen} onOpenChange={setStrategyOpen}>
        <DialogContent className="max-w-2xl max-h-[82vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>模拟盘策略选择</DialogTitle>
            <DialogDescription>控制下一次扫描使用哪些策略池策略。未选择时可保持全部启用策略兼容模式。</DialogDescription>
          </DialogHeader>

          <div className="space-y-5">
            <div className="grid grid-cols-3 gap-2">
              {([
                ['all', '全部启用'],
                ['top_n', 'Top N'],
                ['custom', '自定义'],
              ] as const).map(([mode, label]) => (
                <button
                  key={mode}
                  onClick={() => setStrategySelection(prev => ({ ...prev, mode }))}
                  className={`h-9 rounded-lg text-sm font-medium transition-all ${
                    strategySelection.mode === mode
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {strategySelection.mode === 'top_n' && (
              <label className="block text-sm space-y-1">
                <span className="text-muted-foreground">Top N 数量</span>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={strategySelection.top_n}
                  onChange={e => setStrategySelection(prev => ({ ...prev, top_n: Number(e.target.value) || 1 }))}
                  className="w-28 h-9 px-2 rounded-lg border border-border bg-background text-sm"
                />
              </label>
            )}

            <div className="space-y-2">
              <div className="text-sm font-medium">策略池</div>
              {strategyPool.length === 0 ? (
                <div className="text-sm text-muted-foreground py-6 text-center">暂无策略池策略</div>
              ) : (
                <div className="space-y-2">
                  {strategyPool.map(strategy => {
                    const selected = (strategySelection.strategy_codes || []).includes(strategy.code)
                    const disabled = strategySelection.mode !== 'custom'
                    return (
                      <button
                        key={strategy.code}
                        disabled={disabled}
                        onClick={() => toggleStrategyCode(strategy.code)}
                        className={`w-full flex items-center justify-between gap-3 rounded-lg border p-3 text-left transition-all ${
                          selected && strategySelection.mode === 'custom'
                            ? 'border-primary/40 bg-primary/8'
                            : 'border-border bg-background hover:bg-accent/40'
                        } ${disabled ? 'opacity-75 cursor-default' : ''}`}
                      >
                        <div className="min-w-0">
                          <div className="text-sm font-medium truncate">{strategy.name}</div>
                          <div className="text-xs text-muted-foreground font-mono truncate">{strategy.code}</div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          {strategy.ranking?.score != null && (
                            <span className="text-xs text-muted-foreground">{Number(strategy.ranking.score).toFixed(1)}</span>
                          )}
                          <span className={`text-xs px-2 py-0.5 rounded-full ${strategy.enabled ? 'bg-emerald-500/10 text-emerald-600' : 'bg-muted text-muted-foreground'}`}>
                            {strategy.enabled ? '启用' : '停用'}
                          </span>
                          {strategySelection.mode === 'custom' && (
                            <span className={`w-4 h-4 rounded border ${selected ? 'bg-primary border-primary' : 'border-border'}`} />
                          )}
                        </div>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="flex items-center gap-2 pt-1">
              <Button size="sm" onClick={handleSaveStrategySelection} disabled={strategySaving}>
                {strategySaving ? '保存中...' : '保存'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={notifyOpen} onOpenChange={setNotifyOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>跟单通知设置</DialogTitle>
            <DialogDescription>配置模拟盘交易通知，实时跟踪建仓/平仓动作</DialogDescription>
          </DialogHeader>

          <div className="space-y-5">
            {/* 总开关 */}
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium">启用通知</div>
                <div className="text-xs text-muted-foreground">开启后将通过所选渠道推送交易通知</div>
              </div>
              <Switch checked={notifyEnabled} onCheckedChange={setNotifyEnabled} />
            </div>

            {notifyEnabled && (
              <>
                {/* 通知渠道选择 */}
                <div>
                  <div className="text-sm font-medium mb-2">通知渠道</div>
                  {notifyChannels.length === 0 ? (
                    <div className="text-xs text-muted-foreground">暂无可用渠道，请先在设置中配置通知渠道</div>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {notifyChannels.map(ch => (
                        <button
                          key={ch.id}
                          onClick={() => toggleChannel(ch.id)}
                          className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                            selectedChannelIds.has(ch.id)
                              ? 'bg-primary/10 text-primary ring-1 ring-primary/20'
                              : 'bg-muted/50 text-muted-foreground'
                          }`}
                        >
                          {ch.name}
                        </button>
                      ))}
                    </div>
                  )}
                  {selectedChannelIds.size === 0 && notifyChannels.length > 0 && (
                    <div className="text-xs text-muted-foreground mt-1">未选择渠道时将使用默认渠道</div>
                  )}
                </div>

                {/* 通知模式 */}
                <div className="space-y-3">
                  <div className="text-sm font-medium">通知模式</div>
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm">实时交易信号</div>
                      <div className="text-xs text-muted-foreground">建仓/平仓时立即推送</div>
                    </div>
                    <Switch checked={notifyRealtime} onCheckedChange={setNotifyRealtime} />
                  </div>
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm">盘前计划</div>
                      <div className="text-xs text-muted-foreground">每天 09:00 推送当日候选列表</div>
                    </div>
                    <Switch checked={notifyPremarket} onCheckedChange={setNotifyPremarket} />
                  </div>
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm">日终摘要</div>
                      <div className="text-xs text-muted-foreground">每天 15:30 推送当日操作汇总</div>
                    </div>
                    <Switch checked={notifySummary} onCheckedChange={setNotifySummary} />
                  </div>
                </div>
              </>
            )}

            {/* 操作按钮 */}
            <div className="flex items-center gap-2 pt-2">
              <Button size="sm" onClick={handleSaveNotify} disabled={notifySaving}>
                {notifySaving ? '保存中...' : '保存'}
              </Button>
              {notifyEnabled && (
                <Button variant="outline" size="sm" onClick={handleTestNotify} disabled={notifyTesting}>
                  {notifyTesting ? '发送中...' : '测试通知'}
                </Button>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
