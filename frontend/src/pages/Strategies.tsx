import { Fragment, useEffect, useMemo, useState } from 'react'
import { Activity, BarChart3, ChevronDown, ChevronUp, Play, RefreshCw, Save, Trophy } from 'lucide-react'
import {
  backtestsApi,
  recommendationsApi,
  type BacktestEquityPoint,
  type BacktestRun,
  type BacktestStrategyMetric,
  type BacktestTrade,
  type StrategyCatalogItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

const TYPE_LABELS: Record<string, string> = {
  builtin: '内置',
  screener_formula: '选股公式',
  mcp: 'MCP',
  agent: 'Agent',
}

const RISK_LABELS: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
}

function fmtScore(v?: number | null) {
  if (v == null || !Number.isFinite(v)) return '-'
  return v.toFixed(1)
}

function fmtPct(v?: number | null) {
  if (v == null || !Number.isFinite(v)) return '-'
  const prefix = v > 0 ? '+' : ''
  return `${prefix}${v.toFixed(2)}%`
}

function fmtMoney(v?: number | null) {
  if (v == null || !Number.isFinite(v)) return '-'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function dayOffset(days: number) {
  const d = new Date()
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

function equityPath(points: BacktestEquityPoint[]) {
  if (!points.length) return ''
  const values = points.map(p => Number(p.equity || 0))
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  return points.map((p, i) => {
    const x = points.length === 1 ? 0 : (i / (points.length - 1)) * 100
    const y = 42 - ((Number(p.equity || 0) - min) / span) * 36
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`
  }).join(' ')
}

function rankingBadge(item: StrategyCatalogItem) {
  const ranking = item.ranking
  if (!ranking) return <span className="text-xs text-muted-foreground">未验证</span>
  if (ranking.insufficient_samples) {
    return <span className="text-xs px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-600">样本不足</span>
  }
  return <span className="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary">{ranking.status_label || '已验证'}</span>
}

export default function StrategiesPage() {
  const { toast } = useToast()
  const [items, setItems] = useState<StrategyCatalogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [savingCode, setSavingCode] = useState('')
  const [expandedCode, setExpandedCode] = useState('')
  const [drafts, setDrafts] = useState<Record<string, StrategyCatalogItem>>({})
  const [configText, setConfigText] = useState<Record<string, string>>({})
  const [backtestOpen, setBacktestOpen] = useState(false)
  const [backtestMarket, setBacktestMarket] = useState('CN')
  const [backtestStart, setBacktestStart] = useState(dayOffset(-180))
  const [backtestEnd, setBacktestEnd] = useState(dayOffset(0))
  const [backtestCapital, setBacktestCapital] = useState(1000000)
  const [selectedCodes, setSelectedCodes] = useState<string[]>([])
  const [backtestRun, setBacktestRun] = useState<BacktestRun | null>(null)
  const [backtestBusy, setBacktestBusy] = useState(false)
  const [backtestEquity, setBacktestEquity] = useState<BacktestEquityPoint[]>([])
  const [backtestMetrics, setBacktestMetrics] = useState<BacktestStrategyMetric[]>([])
  const [backtestTrades, setBacktestTrades] = useState<BacktestTrade[]>([])

  const load = async () => {
    setLoading(true)
    try {
      const data = await recommendationsApi.listStrategyPool(false)
      const rows = data.items || []
      setItems(rows)
      setDrafts(Object.fromEntries(rows.map(item => [item.code, { ...item, run_config: item.run_config || {} }])))
      setConfigText(Object.fromEntries(rows.map(item => [item.code, JSON.stringify(item.run_config || {}, null, 2)])))
      setSelectedCodes(prev => prev.length ? prev : rows.filter(item => item.enabled).map(item => item.code))
    } catch {
      toast('加载策略池失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const sortedItems = useMemo(() => {
    return [...items].sort((a, b) => {
      const as = a.ranking?.score ?? -1
      const bs = b.ranking?.score ?? -1
      if (bs !== as) return bs - as
      if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
      return a.code.localeCompare(b.code)
    })
  }, [items])

  const summary = useMemo(() => {
    const enabled = items.filter(i => i.enabled).length
    const verified = items.filter(i => (i.ranking?.sample_size || 0) >= 5).length
    const screener = items.filter(i => i.strategy_type === 'screener_formula').length
    return { enabled, verified, screener }
  }, [items])

  const patchDraft = (code: string, patch: Partial<StrategyCatalogItem>) => {
    setDrafts(prev => ({ ...prev, [code]: { ...prev[code], ...patch } }))
  }

  const save = async (code: string) => {
    const draft = drafts[code]
    if (!draft) return
    let runConfig: Record<string, any>
    try {
      runConfig = JSON.parse(configText[code] || '{}')
    } catch {
      toast('运行参数不是合法 JSON', 'error')
      return
    }

    setSavingCode(code)
    try {
      await recommendationsApi.updateStrategyPoolItem(code, {
        enabled: draft.enabled,
        risk_level: draft.risk_level,
        default_weight: Number(draft.default_weight) || 0,
        run_config: runConfig,
        auto_run_enabled: !!draft.auto_run_enabled,
      })
      toast('策略已保存', 'success')
      await load()
    } catch {
      toast('保存失败', 'error')
    } finally {
      setSavingCode('')
    }
  }

  const recalculateRanking = async () => {
    setLoading(true)
    try {
      const data = await recommendationsApi.recalculateStrategyRanking()
      const rows = data.items || []
      setItems(rows)
      setDrafts(Object.fromEntries(rows.map(item => [item.code, { ...item, run_config: item.run_config || {} }])))
      setConfigText(Object.fromEntries(rows.map(item => [item.code, JSON.stringify(item.run_config || {}, null, 2)])))
      toast('策略排名已刷新', 'success')
    } catch {
      toast('刷新排名失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const loadBacktestResults = async (runId: string) => {
    const [equityRes, metricsRes, tradesRes] = await Promise.all([
      backtestsApi.getEquity(runId),
      backtestsApi.getStrategies(runId),
      backtestsApi.getTrades(runId, { limit: 80, include_skipped: true }),
    ])
    setBacktestEquity(equityRes.items || [])
    setBacktestMetrics(metricsRes.items || [])
    setBacktestTrades(tradesRes.items || [])
  }

  const startBacktest = async () => {
    if (!selectedCodes.length) {
      toast('请至少选择一个策略', 'error')
      return
    }
    setBacktestBusy(true)
    setBacktestEquity([])
    setBacktestMetrics([])
    setBacktestTrades([])
    try {
      const run = await backtestsApi.createRun({
        market: backtestMarket,
        start_date: backtestStart,
        end_date: backtestEnd,
        initial_capital: backtestCapital,
        strategy_codes: selectedCodes,
      })
      setBacktestRun(run)
      toast('回测已启动', 'success')
    } catch {
      toast('启动回测失败', 'error')
      setBacktestBusy(false)
    }
  }

  const toggleSelectedCode = (code: string, checked: boolean) => {
    setSelectedCodes(prev => checked ? Array.from(new Set([...prev, code])) : prev.filter(x => x !== code))
  }

  useEffect(() => {
    if (!backtestRun?.id || !['queued', 'running'].includes(backtestRun.status)) {
      return
    }
    const timer = window.setInterval(async () => {
      try {
        const run = await backtestsApi.getRun(backtestRun.id)
        setBacktestRun(run)
        if (!['queued', 'running'].includes(run.status)) {
          window.clearInterval(timer)
          setBacktestBusy(false)
          if (run.status === 'completed') {
            await loadBacktestResults(run.id)
            await load()
            toast('回测完成，排名已更新', 'success')
          } else {
            toast(run.error || '回测失败', 'error')
          }
        }
      } catch {
        window.clearInterval(timer)
        setBacktestBusy(false)
        toast('查询回测状态失败', 'error')
      }
    }, 1800)
    return () => window.clearInterval(timer)
  }, [backtestRun?.id, backtestRun?.status])

  if (loading && items.length === 0) {
    return (
      <div className="flex items-center justify-center py-20">
        <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shrink-0">
            <Trophy className="w-4 h-4 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-bold">策略池</h1>
            <p className="text-xs text-muted-foreground">统一管理内置策略、选股公式策略、MCP 策略和 Agent 策略</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8" onClick={() => setBacktestOpen(v => !v)}>
            <BarChart3 className="w-3.5 h-3.5" />
            <span className="ml-1">回测</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={load} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span className="ml-1">刷新</span>
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={recalculateRanking} disabled={loading}>
            <Activity className="w-3.5 h-3.5" />
            <span className="ml-1">重新排名</span>
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="card p-3">
          <div className="text-xs text-muted-foreground">策略总数</div>
          <div className="text-xl font-bold">{items.length}</div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-muted-foreground">启用策略</div>
          <div className="text-xl font-bold">{summary.enabled}</div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-muted-foreground">已验证策略</div>
          <div className="text-xl font-bold">{summary.verified}</div>
        </div>
        <div className="card p-3">
          <div className="text-xs text-muted-foreground">选股公式策略</div>
          <div className="text-xl font-bold">{summary.screener}</div>
        </div>
      </div>

      {backtestOpen && (
        <div className="card p-4 space-y-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 flex-1">
              <label className="text-xs space-y-1">
                <span className="text-muted-foreground">市场</span>
                <select
                  className="w-full h-9 px-2 rounded-lg border border-border bg-background text-sm"
                  value={backtestMarket}
                  onChange={e => setBacktestMarket(e.target.value)}
                >
                  <option value="CN">A股</option>
                  <option value="HK">港股</option>
                  <option value="US">美股</option>
                </select>
              </label>
              <label className="text-xs space-y-1">
                <span className="text-muted-foreground">开始日期</span>
                <input
                  type="date"
                  className="w-full h-9 px-2 rounded-lg border border-border bg-background text-sm"
                  value={backtestStart}
                  onChange={e => setBacktestStart(e.target.value)}
                />
              </label>
              <label className="text-xs space-y-1">
                <span className="text-muted-foreground">结束日期</span>
                <input
                  type="date"
                  className="w-full h-9 px-2 rounded-lg border border-border bg-background text-sm"
                  value={backtestEnd}
                  onChange={e => setBacktestEnd(e.target.value)}
                />
              </label>
              <label className="text-xs space-y-1">
                <span className="text-muted-foreground">初始资金</span>
                <input
                  type="number"
                  min={1}
                  step={10000}
                  className="w-full h-9 px-2 rounded-lg border border-border bg-background text-sm"
                  value={backtestCapital}
                  onChange={e => setBacktestCapital(Number(e.target.value) || 0)}
                />
              </label>
            </div>
            <Button size="sm" className="h-9" onClick={startBacktest} disabled={backtestBusy}>
              {backtestBusy ? <span className="w-3.5 h-3.5 border-2 border-current/30 border-t-current rounded-full animate-spin" /> : <Play className="w-3.5 h-3.5" />}
              <span className="ml-1">{backtestBusy ? '运行中' : '开始回测'}</span>
            </Button>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setSelectedCodes(items.filter(item => item.enabled).map(item => item.code))}
            >
              全部启用
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setSelectedCodes(sortedItems.filter(item => item.enabled && !item.ranking?.insufficient_samples).slice(0, 5).map(item => item.code))}
            >
              Top 5
            </Button>
            {items.map(item => (
              <label key={item.code} className="h-7 px-2 inline-flex items-center gap-1 rounded-lg border border-border text-xs">
                <input
                  type="checkbox"
                  checked={selectedCodes.includes(item.code)}
                  onChange={e => toggleSelectedCode(item.code, e.target.checked)}
                />
                <span>{item.name}</span>
              </label>
            ))}
          </div>

          {backtestRun && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div className="rounded-lg border border-border p-3">
                  <div className="text-xs text-muted-foreground">状态</div>
                  <div className="font-semibold">{backtestRun.status}</div>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <div className="text-xs text-muted-foreground">总收益</div>
                  <div className="font-semibold">{fmtPct(backtestRun.summary?.total_return_pct)}</div>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <div className="text-xs text-muted-foreground">最大回撤</div>
                  <div className="font-semibold">{fmtPct(backtestRun.summary?.max_drawdown_pct)}</div>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <div className="text-xs text-muted-foreground">胜率</div>
                  <div className="font-semibold">{fmtPct(backtestRun.summary?.win_rate)}</div>
                </div>
                <div className="rounded-lg border border-border p-3">
                  <div className="text-xs text-muted-foreground">交易/跳过</div>
                  <div className="font-semibold">{backtestRun.summary?.total_trades ?? 0}/{backtestRun.summary?.skipped_count ?? 0}</div>
                </div>
              </div>

              {!!backtestEquity.length && (
                <div className="rounded-lg border border-border p-3">
                  <div className="flex items-center justify-between text-xs text-muted-foreground mb-2">
                    <span>权益曲线</span>
                    <span>{fmtMoney(backtestEquity[backtestEquity.length - 1]?.equity)}</span>
                  </div>
                  <svg viewBox="0 0 100 48" className="w-full h-28 overflow-visible">
                    <path d="M 0 44 L 100 44" stroke="currentColor" strokeOpacity="0.12" strokeWidth="1" />
                    <path d={equityPath(backtestEquity)} fill="none" stroke="hsl(var(--primary))" strokeWidth="2" vectorEffect="non-scaling-stroke" />
                  </svg>
                </div>
              )}

              {!!backtestMetrics.length && (
                <div className="overflow-x-auto rounded-lg border border-border">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        <th className="text-left py-2 px-2">策略</th>
                        <th className="text-right py-2 px-2">样本</th>
                        <th className="text-right py-2 px-2">胜率</th>
                        <th className="text-right py-2 px-2">收益</th>
                        <th className="text-right py-2 px-2">均笔</th>
                        <th className="text-right py-2 px-2">回撤</th>
                      </tr>
                    </thead>
                    <tbody>
                      {backtestMetrics.map(metric => (
                        <tr key={metric.strategy_code} className="border-b border-border/50 last:border-0">
                          <td className="py-2 px-2">{metric.strategy_name || metric.strategy_code}</td>
                          <td className="text-right py-2 px-2">{metric.sample_size}</td>
                          <td className="text-right py-2 px-2">{fmtPct(metric.win_rate)}</td>
                          <td className="text-right py-2 px-2">{fmtMoney(metric.total_pnl)}</td>
                          <td className="text-right py-2 px-2">{fmtPct(metric.avg_return_pct)}</td>
                          <td className="text-right py-2 px-2">{fmtPct(metric.max_drawdown_pct)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {!!backtestTrades.length && (
                <div className="overflow-x-auto rounded-lg border border-border">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        <th className="text-left py-2 px-2">标的</th>
                        <th className="text-left py-2 px-2">策略</th>
                        <th className="text-left py-2 px-2">开仓</th>
                        <th className="text-left py-2 px-2">平仓</th>
                        <th className="text-right py-2 px-2">数量</th>
                        <th className="text-right py-2 px-2">收益</th>
                        <th className="text-left py-2 px-2">原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {backtestTrades.map(trade => (
                        <tr key={trade.id} className="border-b border-border/50 last:border-0">
                          <td className="py-2 px-2">{trade.stock_name || trade.stock_symbol}</td>
                          <td className="py-2 px-2">{trade.strategy_name || trade.strategy_code}</td>
                          <td className="py-2 px-2">{trade.entry_date}</td>
                          <td className="py-2 px-2">{trade.exit_date}</td>
                          <td className="text-right py-2 px-2">{trade.quantity}</td>
                          <td className="text-right py-2 px-2">{trade.skipped ? '-' : fmtPct(trade.pnl_pct)}</td>
                          <td className="py-2 px-2">{trade.skipped ? trade.skip_reason : trade.exit_reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="card p-4">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs">
                <th className="text-left py-2 pr-3">策略</th>
                <th className="text-left py-2 px-2">类型</th>
                <th className="text-right py-2 px-2">综合分</th>
                <th className="text-right py-2 px-2">样本</th>
                <th className="text-right py-2 px-2">胜率</th>
                <th className="text-right py-2 px-2">收益</th>
                <th className="text-right py-2 px-2">回撤</th>
                <th className="text-center py-2 px-2">状态</th>
                <th className="text-right py-2 pl-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map(item => {
                const draft = drafts[item.code] || item
                const expanded = expandedCode === item.code
                return (
                  <Fragment key={item.code}>
                    <tr className="border-b border-border/50 hover:bg-accent/30">
                      <td className="py-2 pr-3 min-w-[220px]">
                        <div className="font-medium">{item.name}</div>
                        <div className="text-xs text-muted-foreground font-mono">{item.code}</div>
                      </td>
                      <td className="py-2 px-2">
                        <span className="text-xs px-2 py-0.5 rounded-full bg-accent text-muted-foreground">
                          {TYPE_LABELS[item.strategy_type || 'builtin'] || item.strategy_type || '内置'}
                        </span>
                      </td>
                      <td className="text-right py-2 px-2 font-medium">{fmtScore(item.ranking?.score)}</td>
                      <td className="text-right py-2 px-2">{item.ranking?.sample_size ?? 0}</td>
                      <td className="text-right py-2 px-2">{fmtPct(item.ranking?.win_rate)}</td>
                      <td className="text-right py-2 px-2">{fmtPct(item.ranking?.avg_return_pct)}</td>
                      <td className="text-right py-2 px-2">{fmtPct(item.ranking?.max_drawdown_pct)}</td>
                      <td className="text-center py-2 px-2">{rankingBadge(item)}</td>
                      <td className="text-right py-2 pl-2">
                        <div className="flex items-center justify-end gap-1">
                          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setExpandedCode(expanded ? '' : item.code)}>
                            {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                          </Button>
                          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => save(item.code)} disabled={savingCode === item.code}>
                            {savingCode === item.code ? <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                          </Button>
                        </div>
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="border-b border-border/50 bg-accent/20">
                        <td colSpan={9} className="py-4">
                          <div className="grid gap-4 lg:grid-cols-[1fr_1.5fr]">
                            <div className="space-y-3">
                              <div className="grid grid-cols-2 gap-3">
                                <label className="text-xs space-y-1">
                                  <span className="text-muted-foreground">启用</span>
                                  <div><Switch checked={!!draft.enabled} onCheckedChange={checked => patchDraft(item.code, { enabled: checked })} /></div>
                                </label>
                                <label className="text-xs space-y-1">
                                  <span className="text-muted-foreground">自动运行</span>
                                  <div><Switch checked={!!draft.auto_run_enabled} onCheckedChange={checked => patchDraft(item.code, { auto_run_enabled: checked })} /></div>
                                </label>
                                <label className="text-xs space-y-1">
                                  <span className="text-muted-foreground">风险等级</span>
                                  <select
                                    className="w-full h-8 px-2 rounded-lg border border-border bg-background text-sm"
                                    value={draft.risk_level || 'medium'}
                                    onChange={e => patchDraft(item.code, { risk_level: e.target.value })}
                                  >
                                    {Object.entries(RISK_LABELS).map(([value, label]) => (
                                      <option key={value} value={value}>{label}</option>
                                    ))}
                                  </select>
                                </label>
                                <label className="text-xs space-y-1">
                                  <span className="text-muted-foreground">默认权重</span>
                                  <input
                                    type="number"
                                    min={0}
                                    step={0.01}
                                    className="w-full h-8 px-2 rounded-lg border border-border bg-background text-sm"
                                    value={draft.default_weight ?? 1}
                                    onChange={e => patchDraft(item.code, { default_weight: Number(e.target.value) })}
                                  />
                                </label>
                              </div>
                              <div className="text-xs text-muted-foreground leading-relaxed">{item.description || '暂无描述'}</div>
                            </div>
                            <label className="text-xs space-y-1">
                              <span className="text-muted-foreground">运行参数 JSON</span>
                              <textarea
                                className="w-full min-h-[150px] rounded-lg border border-border bg-background p-3 font-mono text-xs"
                                value={configText[item.code] || '{}'}
                                onChange={e => setConfigText(prev => ({ ...prev, [item.code]: e.target.value }))}
                              />
                              <span className="text-muted-foreground">常用字段：position_pct、signal_ttl_hours、fee_pct、tax_pct、slippage_pct、max_results。</span>
                            </label>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
