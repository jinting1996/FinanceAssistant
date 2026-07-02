import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Download,
  History,
  ListOrdered,
  Loader2,
  MessageSquare,
  Plus,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Trash2,
} from 'lucide-react'
import {
  stocksApi,
  strategyAnalysisApi,
  type StockSearchResult,
  type StrategyOverview,
  type StrategyPoolItem,
  type StrategyPromptItem,
  type StrategyTags,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { BadgeChip } from '@panwatch/biz-ui/components/badge-chip'
import { AiSuggestionBadge } from '@panwatch/biz-ui/components/ai-suggestion-badge'

const MARKET_LABEL: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股' }

const BREAKOUT_META: Record<string, { label: string; cls: string }> = {
  valid: { label: '有效突破', cls: 'bg-rose-500/15 text-rose-500' },
  pending: { label: '突破待确认', cls: 'bg-amber-500/15 text-amber-500' },
  failed: { label: '突破失败', cls: 'bg-emerald-500/15 text-emerald-500' },
  expired: { label: '突破已过期', cls: 'bg-accent/50 text-muted-foreground' },
  none: { label: '不符合', cls: 'bg-accent/50 text-muted-foreground' },
}

const VOLUME_META: Record<string, { label: string; cls: string }> = {
  strong: { label: '放量确认', cls: 'bg-rose-500/15 text-rose-500' },
  weak: { label: '量能不足', cls: 'bg-accent/50 text-muted-foreground' },
}

const num = (v: unknown, d = 2) => (typeof v === 'number' && !Number.isNaN(v) ? v.toFixed(d) : null)

function StrategyBadges({ tags }: { tags?: StrategyTags }) {
  if (!tags || Object.keys(tags).length === 0) return null
  const gap = typeof tags.gap_to_prev_high_pct === 'number' ? tags.gap_to_prev_high_pct : null
  const breakout = tags.breakout && BREAKOUT_META[tags.breakout]
  const volume = tags.volume_confirm && VOLUME_META[tags.volume_confirm]
  const prevHigh = num(tags.prev_high)
  const support = num(tags.support)
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      {breakout && <BadgeChip size="xs" label={breakout.label} className={breakout.cls} />}
      {prevHigh && (
        <BadgeChip size="xs" label={`前高 ${prevHigh}`} className="bg-accent/50 text-muted-foreground" />
      )}
      {gap != null && (
        <BadgeChip
          size="xs"
          label={`距前高 ${gap >= 0 ? '+' : ''}${gap.toFixed(1)}%`}
          className={gap >= 0 ? 'bg-rose-500/15 text-rose-500' : 'bg-emerald-500/15 text-emerald-500'}
        />
      )}
      {tags.pullback_support && (
        <BadgeChip
          size="xs"
          label="回踩支撑"
          title={support ? `支撑位 ${support}` : undefined}
          className="bg-sky-500/15 text-sky-500"
        />
      )}
      {!tags.pullback_support && support && (
        <BadgeChip size="xs" label={`支撑 ${support}`} className="bg-accent/50 text-muted-foreground" />
      )}
      {volume && <BadgeChip size="xs" label={volume.label} className={volume.cls} />}
      {tags.action && (
        <AiSuggestionBadge
          size="xs"
          isAI
          action={tags.action}
          actionLabel={tags.action_label}
          title={tags.reason || undefined}
        />
      )}
    </div>
  )
}
const MARKET_TABS = [
  { value: '', label: '全部' },
  { value: 'CN', label: 'A股' },
  { value: 'HK', label: '港股' },
  { value: 'US', label: '美股' },
]

export default function StrategyAnalysisPanel() {
  const [strategies, setStrategies] = useState<StrategyPromptItem[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [pool, setPool] = useState<StrategyPoolItem[]>([])
  const [lastConvs, setLastConvs] = useState<Record<string, { conversation_id: number; updated_at: string; title: string }>>({})

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [prompt, setPrompt] = useState('')
  const [editingPrompt, setEditingPrompt] = useState(false)

  const [searchQuery, setSearchQuery] = useState('')
  const [searchMarket, setSearchMarket] = useState('')
  const [searchResults, setSearchResults] = useState<StockSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [showDropdown, setShowDropdown] = useState(false)
  const searchTimer = useRef<ReturnType<typeof setTimeout>>()
  const dropdownRef = useRef<HTMLDivElement>(null)

  const [saving, setSaving] = useState(false)
  const [importing, setImporting] = useState(false)
  const [reanalyzing, setReanalyzing] = useState(false)
  const [adding, setAdding] = useState(false)
  const [message, setMessage] = useState('')

  const [overviewOpen, setOverviewOpen] = useState(false)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [overview, setOverview] = useState<StrategyOverview | null>(null)

  const selected = strategies.find((s) => s.id === selectedId) || null

  const applyStrategy = (row: StrategyPromptItem) => {
    setSelectedId(row.id)
    setName(row.name)
    setDescription(row.description || '')
    setPrompt(row.prompt || '')
    setEditingPrompt(false)
  }

  const loadAll = useCallback(async () => {
    const [stratRes, poolRes] = await Promise.all([
      strategyAnalysisApi.listStrategies().catch(() => ({ items: [] })),
      strategyAnalysisApi.listPool().catch(() => ({ items: [] })),
    ])
    const items = stratRes.items || []
    setStrategies(items)
    setPool(poolRes.items || [])
    setSelectedId((prev) => {
      if (prev && items.some((x) => x.id === prev)) return prev
      const first = items[0]
      if (first) applyStrategy(first)
      return first ? first.id : null
    })
  }, [])

  const loadLastConvs = useCallback(async (strategyId: number | null) => {
    if (!strategyId) {
      setLastConvs({})
      return
    }
    try {
      const res = await strategyAnalysisApi.lastConversations(strategyId)
      setLastConvs(res.items || {})
    } catch {
      setLastConvs({})
    }
  }, [])

  const refreshPool = useCallback(async () => {
    try {
      const poolRes = await strategyAnalysisApi.listPool()
      setPool(poolRes.items || [])
    } catch {
      /* ignore */
    }
  }, [])

  useEffect(() => {
    loadAll().catch((e) => setMessage(e?.message || '加载策略分析失败'))
  }, [loadAll])

  // 切换策略时，加载该策略下各票的最近一次对话（用于「查看上次」）
  useEffect(() => {
    loadLastConvs(selectedId)
  }, [selectedId, loadLastConvs])

  // 策略对话产出后 ChatWidget 广播事件，这里刷新徽章 + 最近对话
  useEffect(() => {
    const onAnalyzed = () => {
      refreshPool()
      loadLastConvs(selectedId)
    }
    window.addEventListener('panwatch-strategy-analyzed', onAnalyzed)
    return () => window.removeEventListener('panwatch-strategy-analyzed', onAnalyzed)
  }, [refreshPool, loadLastConvs, selectedId])

  const dirty = useMemo(() => {
    if (!selected) return name.trim() !== '' || prompt.trim() !== ''
    return (
      name !== selected.name ||
      description !== (selected.description || '') ||
      prompt !== (selected.prompt || '')
    )
  }, [selected, name, description, prompt])

  const saveStrategy = async () => {
    if (!name.trim() || !prompt.trim()) {
      setMessage('策略名称和提示词不能为空')
      return
    }
    setSaving(true)
    setMessage('')
    try {
      const payload = { name: name.trim(), description: description.trim(), prompt }
      const saved = selectedId
        ? await strategyAnalysisApi.updateStrategy(selectedId, payload)
        : await strategyAnalysisApi.createStrategy(payload)
      setMessage('策略已保存')
      setEditingPrompt(false)
      await loadAll()
      setSelectedId(saved.id)
    } catch (e: any) {
      setMessage(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const newStrategy = () => {
    setSelectedId(null)
    setName('新策略')
    setDescription('')
    setPrompt('')
    setEditingPrompt(true)
    setMessage('')
  }

  const removeStrategy = async () => {
    if (!selectedId || !selected || selected.is_default) return
    setSaving(true)
    try {
      await strategyAnalysisApi.deleteStrategy(selectedId)
      setMessage('策略已删除')
      setSelectedId(null)
      await loadAll()
    } catch (e: any) {
      setMessage(e?.message || '删除失败')
    } finally {
      setSaving(false)
    }
  }

  const importPositions = async () => {
    setImporting(true)
    setMessage('')
    try {
      const res = await strategyAnalysisApi.importPositions()
      setMessage(`已从持仓导入 ${res.created} 只（共扫描 ${res.scanned} 只）`)
      const poolRes = await strategyAnalysisApi.listPool()
      setPool(poolRes.items || [])
    } catch (e: any) {
      setMessage(e?.message || '导入持仓失败')
    } finally {
      setImporting(false)
    }
  }

  // ---- 股票搜索下拉（复用持仓同款 /stocks/search）----
  const doSearch = async (q: string, market: string) => {
    if (q.trim().length < 1) {
      setSearchResults([])
      setShowDropdown(false)
      return
    }
    setSearching(true)
    try {
      const results = await stocksApi.search(q.trim(), market)
      setSearchResults(results || [])
      setShowDropdown(true)
    } catch {
      setSearchResults([])
    } finally {
      setSearching(false)
    }
  }

  const handleSearchInput = (value: string) => {
    setSearchQuery(value)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => doSearch(value, searchMarket), 400)
  }

  const handleSearchMarketChange = (market: string) => {
    setSearchMarket(market)
    if (searchQuery) doSearch(searchQuery, market)
  }

  const selectStock = async (item: StockSearchResult) => {
    setShowDropdown(false)
    setSearchQuery('')
    setSearchResults([])
    setAdding(true)
    setMessage('')
    try {
      const res = await strategyAnalysisApi.addPoolItem({
        symbol: item.symbol,
        market: item.market,
        name: item.name,
      })
      setMessage(res.created ? `已加入 ${item.name}（${item.symbol}）` : `${item.name} 已在池中`)
      const poolRes = await strategyAnalysisApi.listPool()
      setPool(poolRes.items || [])
    } catch (e: any) {
      setMessage(e?.message || '加入失败')
    } finally {
      setAdding(false)
    }
  }

  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false)
      }
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [])

  const removePoolItem = async (id: number) => {
    try {
      await strategyAnalysisApi.deletePoolItem(id)
      setPool((prev) => prev.filter((p) => p.id !== id))
    } catch (e: any) {
      setMessage(e?.message || '移除失败')
    }
  }

  // ---- 一键重测 = 用当前策略对池内所有股票批量无头分析，回写徽章 ----
  const reanalyzeAll = async () => {
    if (!selectedId) {
      setMessage('请先选择一个策略')
      return
    }
    if (pool.length === 0) {
      setMessage('策略池为空')
      return
    }
    if (!window.confirm(`将用「${selected?.name}」策略重新分析池内全部 ${pool.length} 只股票，会调用多次 AI，确定继续？`)) {
      return
    }
    setReanalyzing(true)
    setMessage('AI 正在批量重测，请稍候…')
    try {
      const res = await strategyAnalysisApi.reanalyzeAll(selectedId)
      const failCount = res.failed?.length || 0
      setMessage(
        `重测完成：成功 ${res.analyzed}/${res.total} 只` +
          (failCount ? `，失败 ${failCount} 只（${res.failed.map((f) => f.symbol).join('、')}）` : ''),
      )
      await refreshPool()
      await loadLastConvs(selectedId)
    } catch (e: any) {
      setMessage(e?.message || '一键重测失败')
    } finally {
      setReanalyzing(false)
    }
  }

  // ---- 一键分析 = 打开策略对话（复用右下角 ChatWidget），首轮分析作为开场 ----
  const openStrategyChat = (item: StrategyPoolItem) => {
    if (!selected) {
      setMessage('请先选择一个策略')
      return
    }
    const opening =
      `请依据「${selected.name}」策略，结合下方提供的当天与最近行情数据，` +
      `判断 ${item.name || item.symbol}（${item.market}:${item.symbol}）当前状态，` +
      `先用一句话给出明确结论（如：有效突破/突破待确认/突破失败/不符合/观望），再分点说明依据（量价、点位、时间）。`
    window.dispatchEvent(
      new CustomEvent('panwatch-open-chat', {
        detail: {
          symbol: item.symbol,
          market: item.market,
          stockName: item.name || item.symbol,
          strategyId: selected.id,
          openingMessage: opening,
        },
      }),
    )
  }

  // 打开总览弹窗：只读缓存快照，不触发 AI
  const openOverview = async () => {
    if (!selectedId) {
      setMessage('请先选择一个策略')
      return
    }
    setOverviewOpen(true)
    setOverviewLoading(true)
    try {
      const res = await strategyAnalysisApi.getOverview(selectedId)
      setOverview(res)
    } catch {
      setOverview(null)
    } finally {
      setOverviewLoading(false)
    }
  }

  // 手动刷新：重新调用 AI 排序并落缓存
  const refreshOverview = async () => {
    if (!selectedId) return
    setOverviewLoading(true)
    try {
      const res = await strategyAnalysisApi.overview(selectedId)
      setOverview(res)
    } catch (e: any) {
      setMessage(e?.message || '总览排序失败')
    } finally {
      setOverviewLoading(false)
    }
  }

  // 打开上一次的策略对话（查看历史结论，不重新分析）
  const openLastConversation = (item: StrategyPoolItem, conversationId: number) => {
    window.dispatchEvent(
      new CustomEvent('panwatch-open-chat', {
        detail: {
          symbol: item.symbol,
          market: item.market,
          stockName: item.name || item.symbol,
          strategyId: selected?.id,
          conversationId,
        },
      }),
    )
  }

  return (
    <section className="card p-4">
      <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2 text-[15px] font-semibold">
            <Sparkles className="h-4 w-4 text-primary" />
            策略 AI 分析（对话式）
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            把持仓或手动加入的股票放进策略池，让 AI 以所选策略为准，结合当天与最近行情逐票分析；点「分析」直接开对话，可继续追问。
          </p>
        </div>
        <Button
          variant="secondary"
          className="shrink-0"
          onClick={openOverview}
          disabled={!selectedId || pool.length === 0}
          title="查看池内排序（点开是上次结果，弹窗里可手动刷新）"
        >
          <ListOrdered className="h-4 w-4" />
          池子总览排序
        </Button>
      </div>

      {message && (
        <div className="mb-3 rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[12px] text-muted-foreground">
          {message}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
        {/* 左：策略配置 */}
        <div className="rounded-lg border border-border/60 p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-[13px] font-semibold">分析策略</div>
            <Button variant="secondary" size="sm" className="h-7 px-2 text-[11px]" onClick={newStrategy}>
              <Plus className="h-3.5 w-3.5" /> 新建
            </Button>
          </div>
          <div className="mb-3 flex flex-wrap gap-1.5">
            {strategies.map((s) => (
              <button
                key={s.id}
                className={`rounded-full border px-2.5 py-1 text-[12px] transition-colors ${
                  selectedId === s.id
                    ? 'border-primary/50 bg-primary/10 text-primary'
                    : 'border-border/60 text-muted-foreground hover:border-primary/40'
                }`}
                onClick={() => applyStrategy(s)}
              >
                {s.name}
                {s.is_default ? ' ·默认' : ''}
              </button>
            ))}
            {strategies.length === 0 && <span className="text-[12px] text-muted-foreground">暂无策略</span>}
          </div>

          <label className="mb-1 block text-[11px] text-muted-foreground">策略名称</label>
          <Input value={name} onChange={(e) => setName(e.target.value)} className="mb-2" />
          <label className="mb-1 block text-[11px] text-muted-foreground">说明</label>
          <Input value={description} onChange={(e) => setDescription(e.target.value)} className="mb-2" />

          <div className="mb-1 flex items-center justify-between">
            <label className="text-[11px] text-muted-foreground">策略提示词（作 AI 的 system prompt）</label>
            <button className="text-[11px] text-primary" onClick={() => setEditingPrompt((v) => !v)}>
              {editingPrompt ? '收起' : '编辑'}
            </button>
          </div>
          {editingPrompt ? (
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              className="min-h-[220px] w-full resize-y rounded-lg border border-border bg-background px-3 py-2 font-mono text-[12px] text-foreground outline-none focus:border-primary/60"
              spellCheck={false}
            />
          ) : (
            <div className="max-h-[120px] overflow-hidden rounded-lg border border-border/50 bg-background/40 px-3 py-2 text-[11px] text-muted-foreground">
              {prompt ? `${prompt.slice(0, 280)}${prompt.length > 280 ? '…' : ''}` : '（空）'}
            </div>
          )}

          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" className="h-8" onClick={saveStrategy} disabled={saving || !dirty}>
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              保存策略
            </Button>
            {selected && !selected.is_default && (
              <Button
                variant="secondary"
                size="sm"
                className="h-8 text-destructive"
                onClick={removeStrategy}
                disabled={saving}
              >
                <Trash2 className="h-3.5 w-3.5" /> 删除
              </Button>
            )}
          </div>
        </div>

        {/* 右：策略池 + 逐票分析入口 */}
        <div className="rounded-lg border border-border/60 p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-[13px] font-semibold">策略池（{pool.length}）</div>
            <div className="flex items-center gap-1.5">
              <Button
                variant="secondary"
                size="sm"
                className="h-7 px-2 text-[11px]"
                onClick={refreshPool}
                title="刷新徽章"
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="secondary"
                size="sm"
                className="h-7 px-2 text-[11px]"
                onClick={importPositions}
                disabled={importing}
              >
                {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                导入持仓
              </Button>
              <Button
                size="sm"
                className="h-7 px-2 text-[11px]"
                onClick={reanalyzeAll}
                disabled={reanalyzing || !selectedId || pool.length === 0}
                title={selectedId ? '用当前策略重新分析池内全部股票' : '请先选择策略'}
              >
                {reanalyzing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                一键重测
              </Button>
            </div>
          </div>

          {/* 搜索添加 */}
          <div className="mb-3">
            <div className="mb-1.5 flex items-center gap-1">
              {MARKET_TABS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => handleSearchMarketChange(opt.value)}
                  className={`rounded px-2 py-0.5 text-[11px] transition-colors ${
                    searchMarket === opt.value
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <div className="relative" ref={dropdownRef}>
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/50" />
              <Input
                value={searchQuery}
                onChange={(e) => handleSearchInput(e.target.value)}
                onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
                placeholder="代码或名称，如 600519 或 茅台"
                className="pl-9"
                autoComplete="off"
              />
              {(searching || adding) && (
                <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-primary" />
              )}
              {showDropdown && searchResults.length > 0 && (
                <div className="absolute z-50 mt-1 max-h-52 w-full overflow-auto rounded-lg border border-border bg-card shadow-lg">
                  {searchResults.map((item) => (
                    <button
                      key={`${item.market}-${item.symbol}`}
                      type="button"
                      onClick={() => selectStock(item)}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] transition-colors hover:bg-accent/50"
                    >
                      <span className="rounded bg-accent/60 px-1 py-0.5 text-[9px] text-muted-foreground">
                        {MARKET_LABEL[item.market] || item.market}
                      </span>
                      <span className="font-mono text-[12px] text-muted-foreground">{item.symbol}</span>
                      <span className="flex-1 text-foreground">{item.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 池列表 */}
          <div className="space-y-1.5">
            {pool.map((p) => (
              <div
                key={p.id}
                className="rounded-lg border border-border/50 bg-background/40 px-2.5 py-2 text-[12px]"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <span className="font-medium text-foreground">{p.name || p.symbol}</span>
                    <span className="ml-1 font-mono text-[11px] text-muted-foreground">{p.symbol}</span>
                    <span className="ml-1 text-[10px] text-muted-foreground">
                      {MARKET_LABEL[p.market] || p.market}
                      {p.source === 'position' ? ' ·持仓' : ''}
                    </span>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {lastConvs[`${p.market}:${p.symbol}`] && (
                      <Button
                        variant="secondary"
                        size="sm"
                        className="h-7 px-2 text-[11px]"
                        onClick={() => openLastConversation(p, lastConvs[`${p.market}:${p.symbol}`].conversation_id)}
                        title={`查看上次分析（${lastConvs[`${p.market}:${p.symbol}`].updated_at}）`}
                      >
                        <History className="h-3.5 w-3.5" /> 上次
                      </Button>
                    )}
                    <Button
                      size="sm"
                      className="h-7 px-2 text-[11px]"
                      onClick={() => openStrategyChat(p)}
                      disabled={!selectedId}
                      title={selectedId ? '用所选策略开新的分析对话' : '请先选择策略'}
                    >
                      <MessageSquare className="h-3.5 w-3.5" />
                      {lastConvs[`${p.market}:${p.symbol}`] ? '重测' : '分析'}
                    </Button>
                    <button
                      className="text-muted-foreground hover:text-destructive"
                      onClick={() => removePoolItem(p.id)}
                      title="移除"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                <StrategyBadges tags={p.tags} />
              </div>
            ))}
            {pool.length === 0 && (
              <div className="rounded-lg border border-dashed border-border/60 px-3 py-8 text-center text-[12px] text-muted-foreground">
                池子为空，点「导入持仓」或搜索代码添加，再对每只票点「分析」开对话。
              </div>
            )}
          </div>
        </div>
      </div>

      <Dialog open={overviewOpen} onOpenChange={setOverviewOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center justify-between gap-2 pr-8">
              <span className="flex items-center gap-2">
                <ListOrdered className="h-4 w-4 text-primary" />
                池子总览排序{selected ? ` · ${selected.name}` : ''}
              </span>
              <Button
                variant="secondary"
                size="sm"
                className="h-7 px-2 text-[11px]"
                onClick={refreshOverview}
                disabled={overviewLoading}
                title="重新调用 AI 排序"
              >
                {overviewLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                刷新排序
              </Button>
            </DialogTitle>
          </DialogHeader>

          {overviewLoading && (
            <div className="flex items-center justify-center gap-2 py-12 text-[13px] text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> AI 正在汇总排序…
            </div>
          )}

          {!overviewLoading && overview && overview.ranked.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-10 text-center text-[13px] text-muted-foreground">
              <div>还没有排序结果。点「刷新排序」让 AI 汇总池内各票结论并排序。</div>
              <Button onClick={refreshOverview} disabled={overviewLoading}>
                <ListOrdered className="h-4 w-4" /> 开始排序
              </Button>
            </div>
          )}

          {!overviewLoading && overview && overview.ranked.length > 0 && (
            <div className="max-h-[70vh] space-y-3 overflow-auto">
              {overview.summary && (
                <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[12px] leading-relaxed text-foreground">
                  {overview.summary}
                </div>
              )}
              <div className="space-y-2">
                {overview.ranked.map((r) => (
                  <div
                    key={`${r.market}:${r.symbol}`}
                    className="rounded-lg border border-border/50 bg-background/40 px-3 py-2"
                  >
                    <div className="flex items-center gap-2">
                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[12px] font-semibold text-primary">
                        {r.rank}
                      </span>
                      <span className="font-medium text-foreground">{r.name}</span>
                      <span className="font-mono text-[11px] text-muted-foreground">{r.symbol}</span>
                      {typeof r.score === 'number' && (
                        <span className="ml-auto text-[12px] font-semibold text-primary">{r.score} 分</span>
                      )}
                    </div>
                    <StrategyBadges tags={r.tags} />
                    {r.reason && <div className="mt-1.5 text-[12px] text-muted-foreground">{r.reason}</div>}
                  </div>
                ))}
              </div>
              {overview.unanalyzed.length > 0 && (
                <div className="rounded-lg border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-600">
                  以下 {overview.unanalyzed.length} 只尚未分析、未参与排序：
                  {overview.unanalyzed.map((u) => ` ${u.name}(${u.symbol})`).join('、')}
                </div>
              )}
              <div className="text-[10px] text-muted-foreground">
                排序时间 {overview.analyzed_at || '—'}
                {overview.model ? ` · ${overview.model}` : ''}（点右上「刷新排序」重算）
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </section>
  )
}
