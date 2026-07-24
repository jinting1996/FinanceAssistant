import { useEffect, useMemo, useRef, useState } from 'react'
import { Loader2, Pin, PinOff, Plus, RefreshCw, Sprout, Trash2 } from 'lucide-react'
import {
  marketEventsApi,
  type BoardEventMarkItem,
  type BoardEventType,
  type BoardPoolResponse,
  type BoardSignalSummary,
  type BoardValuationDetail,
  type PoolBoardItem,
  type ValuationLabel,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import InteractiveKline, {
  EVENT_MARKER_STYLES,
  type KlineEventMarker,
} from '@panwatch/biz-ui/components/InteractiveKline'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

type PeriodKey = '120d' | '1y' | '3y' | '5y'

const PERIODS: Array<{ key: PeriodKey; label: string; days: number; interval: '1d' | '1w' }> = [
  { key: '120d', label: '120日', days: 120, interval: '1d' },
  { key: '1y', label: '1年', days: 250, interval: '1d' },
  { key: '3y', label: '3年', days: 750, interval: '1w' },
  { key: '5y', label: '5年', days: 1250, interval: '1w' },
]

const EVENT_TYPE_OPTIONS: Array<{ value: BoardEventType; label: string }> = [
  { value: 'policy', label: '政策' },
  { value: 'industry', label: '产业' },
  { value: 'earnings', label: '业绩' },
  { value: 'macro', label: '宏观' },
  { value: 'case', label: '标注' },
]

function changeColor(pct: number | null | undefined) {
  if (pct == null) return 'text-muted-foreground'
  if (pct > 0) return 'text-rose-500'
  if (pct < 0) return 'text-emerald-500'
  return 'text-muted-foreground'
}

function formatPct(pct: number | null | undefined) {
  if (pct == null) return '--'
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
}

const VALUATION_STYLE: Record<ValuationLabel, { text: string; cls: string }> = {
  low: { text: '低估', cls: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400' },
  fair: { text: '合理', cls: 'bg-accent/60 text-muted-foreground' },
  high: { text: '高估', cls: 'bg-rose-500/12 text-rose-600 dark:text-rose-400' },
  unknown: { text: '估值—', cls: 'bg-accent/40 text-muted-foreground' },
}

/** 估值分位徽章文案:优先3年,回落到5年;分位缺失时只显示标签。 */
function valuationBadge(v: BoardValuationDetail | null) {
  if (!v || !v.available) return null
  const label = (v.label || 'unknown') as ValuationLabel
  const pct = v.pe_percentile?.['3y'] ?? v.pe_percentile?.['5y'] ?? null
  const style = VALUATION_STYLE[label] || VALUATION_STYLE.unknown
  const pctText = pct == null ? '' : `PE分位 ${pct.toFixed(0)}% · `
  return { text: `${pctText}${style.text}`, cls: style.cls }
}

export default function SectorPoolPage() {
  const { toast } = useToast()
  const [pool, setPool] = useState<BoardPoolResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [seeding, setSeeding] = useState(false)
  const [selectedCode, setSelectedCode] = useState('')
  const [period, setPeriod] = useState<PeriodKey>('120d')
  const [signal, setSignal] = useState<BoardSignalSummary | null>(null)
  const [valuation, setValuation] = useState<BoardValuationDetail | null>(null)
  const [events, setEvents] = useState<BoardEventMarkItem[]>([])
  const [focusDate, setFocusDate] = useState<string | null>(null)
  const [highlightId, setHighlightId] = useState<number | null>(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [savingEvent, setSavingEvent] = useState(false)
  const [form, setForm] = useState({
    date: '',
    event_type: 'case' as BoardEventType,
    title: '',
    summary: '',
    important: false,
  })
  const timelineRef = useRef<HTMLDivElement | null>(null)

  const allBoards = useMemo(
    () => (pool?.categories || []).flatMap(c => c.boards),
    [pool],
  )
  const selected = useMemo(
    () => allBoards.find(b => b.board_code === selectedCode) || null,
    [allBoards, selectedCode],
  )
  const periodConf = PERIODS.find(p => p.key === period) || PERIODS[0]

  const loadPool = async () => {
    setLoading(true)
    try {
      const res = await marketEventsApi.boardPool()
      setPool(res)
      const boards = (res.categories || []).flatMap(c => c.boards)
      if (boards.length && !boards.some(b => b.board_code === selectedCode)) {
        setSelectedCode(boards[0].board_code)
      }
    } catch (e) {
      toast(e instanceof Error ? `板块池加载失败: ${e.message}` : '板块池加载失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const loadDetail = async (code: string) => {
    setSignal(null)
    setValuation(null)
    setEvents([])
    setFocusDate(null)
    setHighlightId(null)
    try {
      const [sig, evs] = await Promise.all([
        marketEventsApi.boardSignals(code, { days: 120 }),
        marketEventsApi.listBoardEvents(code),
      ])
      setSignal(sig)
      setEvents(evs)
    } catch {
      // 信号/事件加载失败不阻塞K线展示
    }
    // 估值单独拉取,不阻塞也不影响信号/事件
    try {
      setValuation(await marketEventsApi.boardValuation(code))
    } catch {
      setValuation(null)
    }
  }

  useEffect(() => {
    void loadPool()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (selectedCode) void loadDetail(selectedCode)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCode])

  const handleReseed = async () => {
    setSeeding(true)
    try {
      const report = await marketEventsApi.reseedBoardPool()
      toast(`播种完成:新增 ${report?.created ?? 0} 个,未解析 ${report?.unresolved?.length ?? 0} 个`, 'success')
      await loadPool()
    } catch (e) {
      toast(e instanceof Error ? `播种失败: ${e.message}` : '播种失败', 'error')
    } finally {
      setSeeding(false)
    }
  }

  const handleTogglePin = async (board: PoolBoardItem) => {
    const nextTier = board.tier === 'pinned' ? 'pool' : 'pinned'
    try {
      await marketEventsApi.updatePoolBoard(board.board_code, { tier: nextTier })
      setPool(prev =>
        prev
          ? {
              ...prev,
              categories: prev.categories.map(c => ({
                ...c,
                boards: c.boards.map(b =>
                  b.board_code === board.board_code ? { ...b, tier: nextTier } : b,
                ),
              })),
            }
          : prev,
      )
      toast(nextTier === 'pinned' ? '已加入重点关注' : '已取消重点关注', 'success')
    } catch (e) {
      toast(e instanceof Error ? `操作失败: ${e.message}` : '操作失败', 'error')
    }
  }

  const handleCreateEvent = async () => {
    if (!selectedCode || !form.date || !form.title.trim()) {
      toast('日期和标题不能为空', 'error')
      return
    }
    setSavingEvent(true)
    try {
      const created = await marketEventsApi.createBoardEvent(selectedCode, {
        date: form.date,
        event_type: form.event_type,
        title: form.title.trim(),
        summary: form.summary.trim() || undefined,
        importance: form.important ? 2 : 1,
      })
      setEvents(prev => [...prev, created].sort((a, b) => a.date.localeCompare(b.date)))
      setForm({ date: '', event_type: 'case', title: '', summary: '', important: false })
      setShowAddForm(false)
      toast('事件标注已添加', 'success')
    } catch (e) {
      toast(e instanceof Error ? `添加失败: ${e.message}` : '添加失败', 'error')
    } finally {
      setSavingEvent(false)
    }
  }

  const handleDeleteEvent = async (id: number) => {
    try {
      await marketEventsApi.deleteBoardEvent(id)
      setEvents(prev => prev.filter(e => e.id !== id))
    } catch (e) {
      toast(e instanceof Error ? `删除失败: ${e.message}` : '删除失败', 'error')
    }
  }

  const eventMarkers: KlineEventMarker[] = useMemo(
    () =>
      events.map(e => ({
        id: e.id,
        date: e.date,
        event_type: e.event_type,
        title: e.title,
        summary: e.summary,
        importance: e.importance,
      })),
    [events],
  )

  const timelineEvents = useMemo(() => events.slice().reverse(), [events])

  const pinnedBoards = useMemo(() => allBoards.filter(b => b.tier === 'pinned'), [allBoards])

  const renderBoardRow = (board: PoolBoardItem, keyPrefix = '') => {
    const active = board.board_code === selectedCode
    return (
      <button
        key={`${keyPrefix}${board.board_code}`}
        type="button"
        onClick={() => setSelectedCode(board.board_code)}
        className={`w-full flex items-center justify-between gap-1 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors ${
          active ? 'bg-primary/10 text-primary font-medium' : 'hover:bg-accent/50 text-foreground'
        }`}
      >
        <span className="flex items-center gap-1 min-w-0">
          {board.tier === 'pinned' ? <Pin className="w-3 h-3 shrink-0 text-amber-500" /> : null}
          {board.valuation?.label === 'low' ? (
            <span className="shrink-0 h-1.5 w-1.5 rounded-full bg-emerald-500" title="估值偏低" />
          ) : board.valuation?.label === 'high' ? (
            <span className="shrink-0 h-1.5 w-1.5 rounded-full bg-rose-500" title="估值偏高" />
          ) : null}
          <span className="truncate">{board.board_name}</span>
          {board.scope === 'concept' ? (
            <span className="shrink-0 rounded bg-violet-500/10 px-1 text-[10px] text-violet-500">概念</span>
          ) : null}
        </span>
        <span className={`font-mono text-[11px] shrink-0 ${changeColor(board.change_pct)}`}>
          {formatPct(board.change_pct)}
        </span>
      </button>
    )
  }

  const handleMarkerClick = (marker: KlineEventMarker) => {
    if (marker.id == null) return
    setHighlightId(marker.id)
    const el = timelineRef.current?.querySelector(`[data-event-id="${marker.id}"]`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-foreground">板块池</h1>
          <p className="text-[12px] text-muted-foreground">
            按投资体系分类的全量大板块地图:走势、事件、主线线索
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" className="h-8" onClick={() => void handleReseed()} disabled={seeding}>
            {seeding ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sprout className="w-3.5 h-3.5" />}
            重新播种
          </Button>
          <Button variant="secondary" size="sm" className="h-8" onClick={() => void loadPool()} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[230px_minmax(0,1fr)] gap-4 items-start">
        {/* 左侧:分类树 */}
        <div className="card p-2 md:sticky md:top-4 max-h-[80vh] overflow-y-auto">
          {loading && !pool ? (
            <div className="p-4 text-[12px] text-muted-foreground flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" /> 加载板块池…
            </div>
          ) : !allBoards.length ? (
            <div className="p-4 text-[12px] text-muted-foreground">
              板块池为空,点击「重新播种」自动按体系分类初始化。
            </div>
          ) : (
            <>
              {pinnedBoards.length ? (
                <div className="mb-2 border-b border-border/50 pb-2">
                  <div className="px-2 py-1 text-[11px] font-semibold text-amber-600 dark:text-amber-500">
                    重点关注(与事件页关注板块池互通)
                  </div>
                  {pinnedBoards.map(board => renderBoardRow(board, 'pinned-'))}
                </div>
              ) : null}
              {(pool?.categories || []).map(cat => (
                <div key={cat.key} className="mb-2">
                  <div className="px-2 py-1 text-[11px] font-semibold text-muted-foreground">{cat.label}</div>
                  {cat.boards.map(board => renderBoardRow(board))}
                </div>
              ))}
            </>
          )}
        </div>

        {/* 右侧:板块详情 */}
        <div className="space-y-3 min-w-0">
          {selected ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[15px] font-semibold text-foreground">{selected.board_name}</span>
                {selected.scope === 'concept' ? (
                  <span className="rounded bg-violet-500/10 px-2 py-0.5 text-[11px] text-violet-500">概念板块</span>
                ) : (
                  <span className="rounded bg-blue-500/10 px-2 py-0.5 text-[11px] text-blue-500">行业板块</span>
                )}
                {(() => {
                  const badge = valuationBadge(valuation)
                  if (!badge) return null
                  const pb = valuation?.pb_percentile?.['3y'] ?? valuation?.pb_percentile?.['5y'] ?? null
                  const title = valuation?.available
                    ? `PE ${valuation.pe?.toFixed(1)}${pb != null ? ` · PB分位 ${pb.toFixed(0)}%` : ''} · 基于${valuation.history_days ?? 0}个交易日`
                    : ''
                  return (
                    <span className={`rounded px-2 py-0.5 text-[11px] ${badge.cls}`} title={title}>
                      {badge.text}
                    </span>
                  )
                })()}
                {selected.scope === 'industry' && valuation && !valuation.available ? (
                  <span className="rounded bg-accent/40 px-2 py-0.5 text-[11px] text-muted-foreground" title={valuation.reason || ''}>
                    估值待回填
                  </span>
                ) : null}
                {(selected.tags || []).map(tag => (
                  <span key={tag} className="rounded bg-accent/60 px-2 py-0.5 text-[11px] text-muted-foreground">
                    {tag}
                  </span>
                ))}
                {signal?.available ? (
                  <>
                    <span className="rounded bg-accent/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                      {signal.rotation_label}
                    </span>
                    <span className="rounded bg-accent/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                      {signal.macd_label}
                    </span>
                  </>
                ) : null}
                <div className="ml-auto flex items-center gap-2">
                  <div className="inline-flex rounded-lg border border-border/60 bg-accent/20 p-0.5">
                    {PERIODS.map(p => (
                      <button
                        key={p.key}
                        type="button"
                        className={`h-7 min-w-[44px] rounded-md px-2.5 text-[12px] transition-colors ${
                          period === p.key
                            ? 'bg-primary text-primary-foreground'
                            : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                        }`}
                        onClick={() => setPeriod(p.key)}
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                  <Button
                    variant={selected.tier === 'pinned' ? 'default' : 'secondary'}
                    size="sm"
                    className="h-8"
                    onClick={() => void handleTogglePin(selected)}
                  >
                    {selected.tier === 'pinned' ? <PinOff className="w-3.5 h-3.5" /> : <Pin className="w-3.5 h-3.5" />}
                    {selected.tier === 'pinned' ? '取消关注' : '重点关注'}
                  </Button>
                </div>
              </div>

              {signal?.summary ? (
                <div className="rounded-lg border border-border/50 bg-accent/10 px-3 py-2 text-[12px] text-muted-foreground">
                  {signal.summary}
                </div>
              ) : null}

              <InteractiveKline
                key={`${selected.board_code}-${period}`}
                symbol={selected.board_code}
                market="CN"
                initialInterval={periodConf.interval}
                initialDays={String(periodConf.days)}
                availableIntervals={[periodConf.interval]}
                density="compact"
                eventMarkers={eventMarkers}
                onEventMarkerClick={handleMarkerClick}
                focusDate={focusDate}
                endpointBuilder={({ symbol, days, interval }) =>
                  `/market-events/boards/${encodeURIComponent(symbol)}/kline?market=CN&days=${encodeURIComponent(
                    String(days),
                  )}&interval=${encodeURIComponent(interval)}`
                }
              />

              {/* 事件时间轴 */}
              <div className="card p-3" ref={timelineRef}>
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[12px] font-semibold text-foreground">
                    事件时间轴
                    <span className="ml-2 font-normal text-[11px] text-muted-foreground">
                      点击条目定位K线;政策/产业/业绩事件沉淀成板块的历史案例
                    </span>
                  </div>
                  <Button variant="secondary" size="sm" className="h-7" onClick={() => setShowAddForm(v => !v)}>
                    <Plus className="w-3.5 h-3.5" /> 添加标注
                  </Button>
                </div>

                {showAddForm ? (
                  <div className="mb-3 grid grid-cols-1 md:grid-cols-[150px_100px_1fr_auto] gap-2 rounded-lg border border-border/50 bg-accent/10 p-2">
                    <Input
                      type="date"
                      className="h-8 text-[12px]"
                      value={form.date}
                      onChange={e => setForm(f => ({ ...f, date: e.target.value }))}
                    />
                    <select
                      className="h-8 rounded-md border border-input bg-background px-2 text-[12px]"
                      value={form.event_type}
                      onChange={e => setForm(f => ({ ...f, event_type: e.target.value as BoardEventType }))}
                    >
                      {EVENT_TYPE_OPTIONS.map(o => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <div className="space-y-2">
                      <Input
                        className="h-8 text-[12px]"
                        placeholder="标题,如:2021 政策底(双碳目标发布)"
                        value={form.title}
                        onChange={e => setForm(f => ({ ...f, title: e.target.value }))}
                      />
                      <Input
                        className="h-8 text-[12px]"
                        placeholder="简述(可选):催化逻辑、后续验证点"
                        value={form.summary}
                        onChange={e => setForm(f => ({ ...f, summary: e.target.value }))}
                      />
                      <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={form.important}
                          onChange={e => setForm(f => ({ ...f, important: e.target.checked }))}
                        />
                        重要事件(3年/5年宽视野下仍显示)
                      </label>
                    </div>
                    <Button size="sm" className="h-8 self-start" onClick={() => void handleCreateEvent()} disabled={savingEvent}>
                      {savingEvent ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : '保存'}
                    </Button>
                  </div>
                ) : null}

                {!timelineEvents.length ? (
                  <div className="py-4 text-center text-[12px] text-muted-foreground">
                    暂无事件标注。可手工添加历史关键节点(政策底、产业催化、业绩拐点),它们会显示在K线对应位置。
                  </div>
                ) : (
                  <div className="max-h-[320px] space-y-0.5 overflow-y-auto">
                    {timelineEvents.map(ev => {
                      const style = EVENT_MARKER_STYLES[ev.event_type] || EVENT_MARKER_STYLES.case
                      return (
                        <div
                          key={ev.id}
                          data-event-id={ev.id}
                          className={`group flex items-baseline gap-2 rounded-md px-2 py-1.5 text-[12px] transition-colors cursor-pointer ${
                            highlightId === ev.id ? 'bg-primary/10' : 'hover:bg-accent/40'
                          }`}
                          onClick={() => {
                            setHighlightId(ev.id)
                            setFocusDate(ev.date)
                          }}
                        >
                          <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{ev.date}</span>
                          <span
                            className="shrink-0 rounded px-1.5 py-px text-[10px] text-white"
                            style={{ backgroundColor: style.color }}
                          >
                            {style.label}
                          </span>
                          {ev.importance >= 2 ? (
                            <span className="shrink-0 rounded bg-amber-500/15 px-1 text-[10px] text-amber-600">重要</span>
                          ) : null}
                          <span className="min-w-0">
                            <span className="text-foreground">{ev.title}</span>
                            {ev.summary ? (
                              <span className="ml-1.5 text-[11px] text-muted-foreground">{ev.summary}</span>
                            ) : null}
                          </span>
                          <button
                            type="button"
                            className="ml-auto hidden shrink-0 text-muted-foreground hover:text-rose-500 group-hover:block"
                            onClick={e => {
                              e.stopPropagation()
                              void handleDeleteEvent(ev.id)
                            }}
                            aria-label="删除标注"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="card p-8 text-center text-[13px] text-muted-foreground">
              {loading ? '加载中…' : '在左侧选择一个板块查看走势与事件'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
