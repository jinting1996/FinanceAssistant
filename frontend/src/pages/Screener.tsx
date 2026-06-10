import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, CheckCircle2, Code2, Eye, LineChart, Loader2, Play, Plus, RefreshCw, Save, Search, Trash2 } from 'lucide-react'
import {
  marketEventsApi,
  screenerApi,
  stocksApi,
  type ScreenerFormulaItem,
  type ScreenerFunctionCatalog,
  type ScreenerProviderCatalogItem,
  type ScreenerResultItem,
  type ScreenerRunItem,
  type WatchedBoardItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import KlineModal from '@panwatch/biz-ui/components/KlineModal'
import { DeepAnalysisModal } from '@panwatch/biz-ui/components/deep-analysis-modal'

const DEFAULT_FORMULA = 'CROSS(MA(C,5), MA(C,20)) AND RSI(C,6) < 70'

const fmt = (value: unknown, digits = 2) => {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--'
  return value.toFixed(digits)
}

const pctClass = (value: number | null | undefined) => {
  if (value == null) return 'text-muted-foreground'
  return value >= 0 ? 'text-rose-500' : 'text-emerald-500'
}

const buildUniverseConfig = (args: {
  provider: string
  includeWatchlist: boolean
  includeBoards: boolean
  boardCodes: string[]
}) => ({
  market: 'CN',
  provider: args.provider,
  include_watchlist: args.includeWatchlist,
  include_watched_boards: args.includeBoards,
  board_codes: args.boardCodes,
  max_symbols: 300,
  days: 120,
})

function ResultActions({
  item,
  onOpenKline,
  onOpenAnalysis,
}: {
  item: ScreenerResultItem
  onOpenKline: (item: ScreenerResultItem) => void
  onOpenAnalysis: (item: ScreenerResultItem) => void
}) {
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)
  const [error, setError] = useState('')

  const addToWatchlist = async () => {
    setAdding(true)
    setError('')
    try {
      await stocksApi.create({ symbol: item.symbol, market: item.market || 'CN', name: item.name || item.symbol })
      setAdded(true)
    } catch (e: any) {
      setError(e?.message || '加入失败')
    } finally {
      setAdding(false)
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center justify-end gap-1.5">
        <Button variant="secondary" size="sm" className="h-8 px-2" onClick={addToWatchlist} disabled={adding || added}>
          {adding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : added ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {added ? '已加入' : '自选'}
        </Button>
        <Button variant="secondary" size="sm" className="h-8 px-2" onClick={() => onOpenKline(item)}>
          <LineChart className="h-3.5 w-3.5" />
          K线
        </Button>
        <Button size="sm" className="h-8 px-2" onClick={() => onOpenAnalysis(item)}>
          <Bot className="h-3.5 w-3.5" />
          深度分析
        </Button>
      </div>
      {error && <div className="text-[11px] text-rose-500">{error}</div>}
    </div>
  )
}

export default function ScreenerPage() {
  const [catalog, setCatalog] = useState<ScreenerFunctionCatalog | null>(null)
  const [providers, setProviders] = useState<ScreenerProviderCatalogItem[]>([])
  const [formulas, setFormulas] = useState<ScreenerFormulaItem[]>([])
  const [boards, setBoards] = useState<WatchedBoardItem[]>([])
  const [runs, setRuns] = useState<ScreenerRunItem[]>([])
  const [selectedFormulaId, setSelectedFormulaId] = useState<number | null>(null)
  const [name, setName] = useState('均线金叉未过热')
  const [description, setDescription] = useState('MA5 上穿 MA20，同时 RSI6 未进入过热区间')
  const [formula, setFormula] = useState(DEFAULT_FORMULA)
  const [includeWatchlist, setIncludeWatchlist] = useState(true)
  const [includeBoards, setIncludeBoards] = useState(true)
  const [provider, setProvider] = useState('panwatch')
  const [selectedBoards, setSelectedBoards] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [validating, setValidating] = useState(false)
  const [message, setMessage] = useState('')
  const [currentRun, setCurrentRun] = useState<ScreenerRunItem | null>(null)
  const [klineTarget, setKlineTarget] = useState<ScreenerResultItem | null>(null)
  const [analysisTarget, setAnalysisTarget] = useState<ScreenerResultItem | null>(null)
  const pollRef = useRef<ReturnType<typeof window.setInterval> | null>(null)

  const selectedProvider = providers.find((x) => x.name === provider)
  const providerAvailable = selectedProvider ? selectedProvider.available : provider === 'panwatch'

  const clearPoll = useCallback(() => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const loadInitial = useCallback(async () => {
    const [funcRes, providerRes, formulaRes, boardRes, runRes] = await Promise.all([
      screenerApi.functions().catch(() => null),
      screenerApi.providerCatalog().catch(() => ({ items: [] })),
      screenerApi.listFormulas().catch(() => ({ items: [] })),
      marketEventsApi.listWatchedBoards().catch(() => []),
      screenerApi.listRuns(10).catch(() => ({ items: [] })),
    ])
    setCatalog(funcRes)
    setProviders(providerRes.items || [])
    setFormulas(formulaRes.items || [])
    setBoards(boardRes || [])
    setRuns(runRes.items || [])
    if ((formulaRes.items || []).length > 0 && selectedFormulaId == null) {
      const first = formulaRes.items[0]
      setSelectedFormulaId(first.id)
      setName(first.name)
      setDescription(first.description || '')
      setFormula(first.formula)
      const cfg = first.universe_config || {}
      setProvider(String(cfg.provider || 'panwatch'))
      setIncludeWatchlist(cfg.include_watchlist !== false)
      setIncludeBoards(cfg.include_watched_boards !== false)
      setSelectedBoards(Array.isArray(cfg.board_codes) ? cfg.board_codes : [])
    }
  }, [selectedFormulaId])

  useEffect(() => {
    loadInitial().catch((e) => setMessage(e?.message || '加载选股配置失败'))
  }, [loadInitial])

  useEffect(() => () => clearPoll(), [clearPoll])

  const universeConfig = useMemo(() => buildUniverseConfig({
    provider,
    includeWatchlist,
    includeBoards,
    boardCodes: selectedBoards,
  }), [provider, includeWatchlist, includeBoards, selectedBoards])

  const selectFormula = (row: ScreenerFormulaItem) => {
    setSelectedFormulaId(row.id)
    setName(row.name)
    setDescription(row.description || '')
    setFormula(row.formula)
    const cfg = row.universe_config || {}
    setProvider(String(cfg.provider || 'panwatch'))
    setIncludeWatchlist(cfg.include_watchlist !== false)
    setIncludeBoards(cfg.include_watched_boards !== false)
    setSelectedBoards(Array.isArray(cfg.board_codes) ? cfg.board_codes : [])
    setMessage('')
  }

  const saveFormula = async () => {
    setSaving(true)
    setMessage('')
    try {
      const payload = { name, description, formula, universe_config: universeConfig, enabled: true }
      const saved = selectedFormulaId
        ? await screenerApi.updateFormula(selectedFormulaId, payload)
        : await screenerApi.createFormula(payload)
      setSelectedFormulaId(saved.id)
      setMessage('公式已保存')
      await loadInitial()
    } catch (e: any) {
      setMessage(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const deleteFormula = async () => {
    if (!selectedFormulaId) return
    setSaving(true)
    try {
      await screenerApi.deleteFormula(selectedFormulaId)
      setSelectedFormulaId(null)
      setName('新选股公式')
      setDescription('')
      setFormula(DEFAULT_FORMULA)
      setProvider('panwatch')
      setMessage('公式已删除')
      await loadInitial()
    } catch (e: any) {
      setMessage(e?.message || '删除失败')
    } finally {
      setSaving(false)
    }
  }

  const validateFormula = async () => {
    setValidating(true)
    try {
      const res = await screenerApi.validateFormula(formula)
      setMessage(res.message)
    } catch (e: any) {
      setMessage(e?.message || '校验失败')
    } finally {
      setValidating(false)
    }
  }

  const pollRun = (runId: number) => {
    clearPoll()
    pollRef.current = window.setInterval(async () => {
      try {
        const row = await screenerApi.getRun(runId)
        setCurrentRun(row)
        if (['success', 'failed', 'cancelled', 'stale'].includes(row.status)) {
          clearPoll()
          setRunning(false)
          screenerApi.listRuns(10).then((res) => setRuns(res.items || [])).catch(() => {})
        }
      } catch (e: any) {
        clearPoll()
        setMessage(e?.message || '读取运行结果失败')
        setRunning(false)
      }
    }, 1800)
  }

  const runScreener = async () => {
    if (running || !providerAvailable) return
    setRunning(true)
    setMessage('')
    setCurrentRun(null)
    try {
      const run = await screenerApi.createRun({
        formula_id: selectedFormulaId,
        formula: selectedFormulaId ? undefined : formula,
        universe_config: universeConfig,
      })
      setCurrentRun(run)
      pollRun(run.id)
    } catch (e: any) {
      setMessage(e?.message || '启动筛选失败')
      setRunning(false)
    }
  }

  const toggleBoard = (code: string) => {
    setSelectedBoards((prev) => (prev.includes(code) ? prev.filter((x) => x !== code) : [...prev, code]))
  }

  const results = currentRun?.results || []
  const progressTotal = currentRun?.progress_total || currentRun?.total_count || 0
  const progressCurrent = currentRun?.progress_current || 0
  const progressPct = progressTotal > 0 ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100)) : 0
  const selectedBoardNames = selectedBoards.length
    ? boards.filter((b) => selectedBoards.includes(b.board_code)).map((b) => b.board_name)
    : boards.map((b) => b.board_name)

  return (
    <div className="mx-auto max-w-7xl space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="text-[12px] text-muted-foreground">公式选股 / 关注池与板块成分股</div>
          <h1 className="mt-1 text-2xl font-semibold tracking-normal text-foreground">选股工作台</h1>
          <p className="mt-2 max-w-2xl text-[13px] text-muted-foreground">
            用通达信风格公式筛选自选股和关注板块成分股，命中后可加入自选、查看 K 线或触发 TradingAgents 单股深度分析。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" onClick={validateFormula} disabled={validating}>
            {validating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Code2 className="h-4 w-4" />}
            校验
          </Button>
          <Button variant="secondary" onClick={saveFormula} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存公式
          </Button>
          <Button onClick={runScreener} disabled={running || !providerAvailable || (!includeWatchlist && !includeBoards)}>
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            运行选股
          </Button>
        </div>
      </div>

      {message && <div className="rounded-lg border border-border/60 bg-card px-3 py-2 text-[12px] text-muted-foreground">{message}</div>}

      <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <div className="space-y-4">
          <section className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2 text-[13px] font-semibold">
                <Search className="h-4 w-4 text-primary" />
                已保存公式
              </div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setSelectedFormulaId(null)
                  setName('新选股公式')
                  setDescription('')
                  setFormula(DEFAULT_FORMULA)
                  setProvider('panwatch')
                }}
              >
                新建
              </Button>
            </div>
            <div className="space-y-2">
              {formulas.length === 0 && (
                <div className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-[12px] text-muted-foreground">
                  暂无保存公式，可以先保存当前默认公式。
                </div>
              )}
              {formulas.map((row) => (
                <button
                  key={row.id}
                  className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                    selectedFormulaId === row.id ? 'border-primary/50 bg-primary/10' : 'border-border/60 bg-background/40 hover:bg-accent/30'
                  }`}
                  onClick={() => selectFormula(row)}
                >
                  <div className="text-[13px] font-medium text-foreground">{row.name}</div>
                  <div className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">{row.formula}</div>
                </button>
              ))}
            </div>
          </section>

          <section className="card p-4">
            <div className="mb-3 text-[13px] font-semibold">股票池</div>
            <label className="mb-2 flex items-center justify-between rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[12px]">
              <span>自选股</span>
              <input type="checkbox" checked={includeWatchlist} onChange={(e) => setIncludeWatchlist(e.target.checked)} />
            </label>
            <label className="mb-3 flex items-center justify-between rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[12px]">
              <span>关注板块成分股</span>
              <input type="checkbox" checked={includeBoards} onChange={(e) => setIncludeBoards(e.target.checked)} />
            </label>
            <div className="mb-2 flex items-center justify-between text-[11px] text-muted-foreground">
              <span>板块选择</span>
              <button className="text-primary" onClick={() => setSelectedBoards([])}>全部关注板块</button>
            </div>
            <div className="flex flex-wrap gap-2">
              {boards.length === 0 && <span className="text-[12px] text-muted-foreground">暂无关注板块，可先在事件页添加。</span>}
              {boards.map((board) => {
                const active = selectedBoards.length === 0 || selectedBoards.includes(board.board_code)
                return (
                  <button
                    key={board.board_code}
                    className={`rounded-full border px-2.5 py-1 text-[12px] transition-colors ${
                      active ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border/60 text-muted-foreground'
                    }`}
                    onClick={() => toggleBoard(board.board_code)}
                  >
                    {board.board_name}
                  </button>
                )
              })}
            </div>
            <div className="mt-3 rounded-lg bg-accent/20 px-3 py-2 text-[11px] text-muted-foreground">
              当前范围：{includeWatchlist ? '自选股' : ''}{includeWatchlist && includeBoards ? ' + ' : ''}{includeBoards ? selectedBoardNames.join('、') || '全部关注板块' : ''}
            </div>
            <div className="mt-3">
              <div className="mb-2 text-[11px] text-muted-foreground">数据源 Provider</div>
              <div className="grid grid-cols-3 gap-2">
                {(providers.length ? providers : [
                  { name: 'panwatch', label: 'PanWatch', status: 'available', available: true, configured: true, description: '', type: 'screener' },
                ]).map((item) => (
                  <button
                    key={item.name}
                    className={`rounded-lg border px-2 py-2 text-left transition-colors ${
                      provider === item.name ? 'border-primary/50 bg-primary/10' : 'border-border/60 bg-background/40'
                    } ${item.available ? '' : 'opacity-60'}`}
                    onClick={() => setProvider(item.name)}
                  >
                    <div className="text-[12px] font-medium">{item.label}</div>
                    <div className="mt-0.5 text-[10px] text-muted-foreground">{item.available ? '可用' : '未配置'}</div>
                  </button>
                ))}
              </div>
              {!providerAvailable && <div className="mt-2 text-[11px] text-amber-500">{selectedProvider?.description || '该 provider 未配置'}</div>}
            </div>
          </section>

          <section className="card p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="text-[13px] font-semibold">最近运行</div>
              <Button variant="secondary" size="sm" onClick={() => screenerApi.listRuns(10).then((res) => setRuns(res.items || []))}>
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="space-y-2">
              {runs.map((run) => (
                <button
                  key={run.id}
                  className="w-full rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-left hover:bg-accent/30"
                  onClick={async () => setCurrentRun(await screenerApi.getRun(run.id))}
                >
                  <div className="flex items-center justify-between text-[12px]">
                    <span>#{run.id} {run.status}</span>
                    <span>{run.matched_count}/{run.total_count}</span>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-muted-foreground">{run.formula_snapshot}</div>
                </button>
              ))}
              {runs.length === 0 && <div className="text-[12px] text-muted-foreground">暂无运行历史。</div>}
            </div>
          </section>
        </div>

        <div className="space-y-4">
          <section className="card p-4">
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-[12px] text-muted-foreground">公式名称</label>
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div>
                <label className="mb-1 block text-[12px] text-muted-foreground">说明</label>
                <Input value={description} onChange={(e) => setDescription(e.target.value)} />
              </div>
            </div>
            <label className="mb-1 mt-3 block text-[12px] text-muted-foreground">通达信风格公式</label>
            <textarea
              value={formula}
              onChange={(e) => setFormula(e.target.value)}
              className="min-h-[150px] w-full resize-y rounded-lg border border-border bg-background px-3 py-2 font-mono text-[13px] text-foreground outline-none focus:border-primary/60"
              spellCheck={false}
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {catalog?.examples?.map((ex) => (
                <button
                  key={ex.name}
                  className="rounded-full border border-border/60 px-2.5 py-1 text-[11px] text-muted-foreground hover:border-primary/40 hover:text-primary"
                  onClick={() => {
                    setName(ex.name)
                    setFormula(ex.formula)
                  }}
                >
                  {ex.name}
                </button>
              ))}
              {selectedFormulaId && (
                <Button variant="secondary" size="sm" className="ml-auto h-7 px-2 text-[11px] text-destructive" onClick={deleteFormula}>
                  <Trash2 className="h-3.5 w-3.5" />
                  删除当前公式
                </Button>
              )}
            </div>
          </section>

          <section className="card overflow-hidden">
            <div className="flex flex-col gap-2 border-b border-border/60 px-4 py-3 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="text-[13px] font-semibold">筛选结果</div>
                <div className="text-[11px] text-muted-foreground">
                  {currentRun ? `运行 #${currentRun.id} · ${currentRun.status} · 命中 ${currentRun.matched_count}/${currentRun.total_count}` : '运行公式后展示命中股票'}
                  {currentRun?.duration_ms ? ` · ${currentRun.duration_ms}ms` : ''}
                </div>
              </div>
              {running && <div className="flex items-center gap-2 text-[12px] text-primary"><Loader2 className="h-4 w-4 animate-spin" /> 正在筛选</div>}
            </div>

            {currentRun && currentRun.status !== 'success' && (
              <div className="border-b border-border/60 px-4 py-3">
                <div className="mb-1 flex justify-between text-[11px] text-muted-foreground">
                  <span>进度</span>
                  <span>{progressCurrent}/{progressTotal || '--'}</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-accent/40">
                  <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${progressPct}%` }} />
                </div>
              </div>
            )}

            {currentRun?.error && (
              <div className="m-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-500">
                {currentRun.error}
              </div>
            )}

            <div className="overflow-x-auto">
              <table className="w-full min-w-[920px] text-left text-[12px]">
                <thead className="bg-accent/30 text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2 font-medium">股票</th>
                    <th className="px-3 py-2 font-medium">来源板块</th>
                    <th className="px-3 py-2 text-right font-medium">收盘</th>
                    <th className="px-3 py-2 text-right font-medium">涨跌</th>
                    <th className="px-3 py-2 font-medium">指标快照</th>
                    <th className="px-4 py-2 text-right font-medium">动作</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((item) => (
                    <tr key={`${item.market}:${item.symbol}`} className="border-t border-border/50">
                      <td className="px-4 py-3">
                        <div className="font-medium text-foreground">{item.name}</div>
                        <div className="font-mono text-[11px] text-muted-foreground">{item.symbol}</div>
                      </td>
                      <td className="px-3 py-3 text-muted-foreground">{item.board_name || '--'}</td>
                      <td className="px-3 py-3 text-right font-mono">{fmt(item.last_close)}</td>
                      <td className={`px-3 py-3 text-right font-mono ${pctClass(item.change_pct)}`}>
                        {item.change_pct == null ? '--' : `${item.change_pct >= 0 ? '+' : ''}${fmt(item.change_pct)}%`}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap gap-1.5">
                          <span className="rounded-full bg-accent/40 px-2 py-0.5">MA5 {fmt(item.indicators?.ma5)}</span>
                          <span className="rounded-full bg-accent/40 px-2 py-0.5">MA20 {fmt(item.indicators?.ma20)}</span>
                          <span className="rounded-full bg-accent/40 px-2 py-0.5">RSI6 {fmt(item.indicators?.rsi6, 1)}</span>
                          <span className="rounded-full bg-accent/40 px-2 py-0.5">MACD {fmt(item.indicators?.macd_hist, 3)}</span>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <ResultActions item={item} onOpenKline={setKlineTarget} onOpenAnalysis={setAnalysisTarget} />
                      </td>
                    </tr>
                  ))}
                  {!results.length && (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-muted-foreground">
                        {currentRun?.status === 'success' ? '本次没有命中股票。' : '暂无结果。'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="card p-4">
            <div className="mb-3 flex items-center gap-2 text-[13px] font-semibold">
              <Eye className="h-4 w-4 text-primary" />
              支持语法
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <div className="mb-2 text-[12px] font-medium">字段</div>
                <div className="flex flex-wrap gap-1.5">
                  {catalog?.fields?.map((item) => <span key={item.name} className="rounded-full bg-accent/40 px-2 py-0.5 text-[11px]">{item.name}</span>)}
                </div>
              </div>
              <div className="rounded-lg border border-border/60 bg-background/40 p-3">
                <div className="mb-2 text-[12px] font-medium">函数</div>
                <div className="flex flex-wrap gap-1.5">
                  {catalog?.functions?.map((item) => <span key={item.name} className="rounded-full bg-accent/40 px-2 py-0.5 text-[11px]">{item.name}</span>)}
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <KlineModal
        open={!!klineTarget}
        onOpenChange={(open) => !open && setKlineTarget(null)}
        symbol={klineTarget?.symbol || ''}
        market={klineTarget?.market || 'CN'}
        title={klineTarget ? `${klineTarget.name} K线` : undefined}
        initialDays="120"
      />
      <DeepAnalysisModal
        open={!!analysisTarget}
        onOpenChange={(open) => !open && setAnalysisTarget(null)}
        stockId={0}
        stockSymbol={analysisTarget?.symbol || ''}
        stockName={analysisTarget?.name || analysisTarget?.symbol || ''}
        stockMarket={analysisTarget?.market || 'CN'}
      />
    </div>
  )
}
