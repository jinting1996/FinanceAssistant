import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Brain,
  CalendarDays,
  Clock3,
  Flame,
  Layers,
  Loader2,
  Newspaper,
  RefreshCw,
  Search,
  Trash2,
  TrendingUp,
  X,
  Zap,
} from 'lucide-react'
import {
  marketEventsApi,
  type BoardSearchItem,
  type BoardSignalSummary,
  type EventSentiment,
  type FlowState,
  type ImpactLevel,
  type MarketCode,
  type MarketEventPeriod,
  type MarketEventsOverview,
  type SectorFlowItem,
  type SectorLeader,
  type WatchedBoardItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'
import { DeepAnalysisModal } from '@panwatch/biz-ui/components/deep-analysis-modal'

const marketLabel: Record<MarketCode, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
}

const periodLabel: Record<MarketEventPeriod, string> = {
  week: '本周',
  month: '本月',
  rolling_month: '近30天',
}

const sentimentLabel: Record<EventSentiment, string> = {
  positive: '偏多',
  negative: '偏空',
  neutral: '中性',
}

const impactLabel: Record<ImpactLevel, string> = {
  high: '高影响',
  medium: '中影响',
  low: '低影响',
}

const macdTone: Record<string, string> = {
  golden_cross: 'text-rose-400 bg-rose-500/12 border-rose-500/25',
  bullish_above_zero: 'text-rose-400 bg-rose-500/12 border-rose-500/25',
  bullish_repair: 'text-amber-300 bg-amber-500/12 border-amber-500/25',
  dead_cross: 'text-emerald-400 bg-emerald-500/12 border-emerald-500/25',
  bearish_below_zero: 'text-emerald-400 bg-emerald-500/12 border-emerald-500/25',
  bearish_fade: 'text-emerald-400 bg-emerald-500/12 border-emerald-500/25',
}

const formatPct = (value: number | null | undefined) => {
  if (value == null || Number.isNaN(value)) return '--'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

const formatAmount = (value: number | null | undefined) => {
  if (value == null || Number.isNaN(value)) return '--'
  const abs = Math.abs(value)
  if (abs >= 100000000) return `${(value / 100000000).toFixed(1)}亿`
  if (abs >= 10000) return `${(value / 10000).toFixed(1)}万`
  return value.toFixed(0)
}

const eventDayKey = (value: string) => (value || '').slice(0, 10) || '未定日期'

const eventTimeLabel = (value: string) => {
  const match = String(value || '').match(/\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2})/)
  return match ? match[1] : '全天'
}

const dateLabel = (dateKey: string) => {
  const d = new Date(`${dateKey}T00:00:00`)
  if (Number.isNaN(d.getTime())) return dateKey
  const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()]
  return `${dateKey.slice(5)} ${weekday}`
}

const sentimentClass = (sentiment: EventSentiment) => {
  if (sentiment === 'positive') return 'bg-rose-500/12 text-rose-400 border-rose-500/25'
  if (sentiment === 'negative') return 'bg-emerald-500/12 text-emerald-400 border-emerald-500/25'
  return 'bg-blue-500/12 text-blue-400 border-blue-500/25'
}

const impactClass = (level: ImpactLevel) => {
  if (level === 'high') return 'bg-amber-500/15 text-amber-300 border-amber-500/30'
  if (level === 'medium') return 'bg-primary/12 text-primary border-primary/25'
  return 'bg-accent text-muted-foreground border-border/50'
}

const flowClass = (state: FlowState) => {
  if (state === 'inflow') return 'bg-rose-500/12 text-rose-400 border-rose-500/25'
  if (state === 'active') return 'bg-amber-500/12 text-amber-300 border-amber-500/25'
  if (state === 'cooling') return 'bg-emerald-500/12 text-emerald-400 border-emerald-500/25'
  return 'bg-accent text-muted-foreground border-border/50'
}

const scoreColor = (score: number) => {
  if (score >= 75) return 'bg-rose-500'
  if (score >= 48) return 'bg-amber-500'
  return 'bg-primary'
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="border border-dashed border-border rounded-lg p-8 text-center text-[13px] text-muted-foreground">
      {text}
    </div>
  )
}

function EventSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="h-32 rounded-lg bg-muted/50 animate-pulse" />
      ))}
    </div>
  )
}

function BoardRow({ board, index }: { board: SectorFlowItem; index: number }) {
  const pct = board.change_pct ?? 0
  const positive = pct >= 0
  return (
    <div className="rounded-lg border border-border/60 bg-background/50 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="w-6 h-6 rounded-md bg-accent text-[11px] font-semibold flex items-center justify-center text-muted-foreground">
              {index + 1}
            </span>
            <div className="font-semibold text-[14px] truncate">{board.name}</div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
            <span className={`px-2 py-1 rounded-md border ${flowClass(board.flow_state)}`}>{board.flow_label}</span>
            <span>成交额 {formatAmount(board.turnover)}</span>
            {board.rank_turnover && <span>成交排名 #{board.rank_turnover}</span>}
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-[16px] font-semibold ${positive ? 'text-rose-400' : 'text-emerald-400'}`}>
            {positive ? <ArrowUpRight className="inline w-4 h-4" /> : <ArrowDownRight className="inline w-4 h-4" />}
            {formatPct(board.change_pct)}
          </div>
          <div className="text-[11px] text-muted-foreground">热度 {board.flow_score.toFixed(1)}</div>
        </div>
      </div>
      <div className="mt-3 h-1.5 rounded-full bg-muted overflow-hidden">
        <div className={`h-full rounded-full ${scoreColor(board.flow_score)}`} style={{ width: `${Math.max(8, Math.min(100, board.flow_score))}%` }} />
      </div>
      <p className="mt-3 text-[12px] leading-5 text-muted-foreground">{board.rotation_signal}</p>
      {board.leaders.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {board.leaders.slice(0, 5).map((leader) => (
            <span key={`${leader.market}:${leader.symbol}`} className="px-2 py-1 rounded-md bg-accent/50 text-[11px] text-muted-foreground">
              {leader.name} {formatPct(leader.change_pct)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function SignalBadge({ label, tone }: { label: string; tone?: string }) {
  return <span className={`px-2 py-1 rounded-md border text-[11px] ${tone || 'bg-accent text-muted-foreground border-border/50'}`}>{label}</span>
}

function WatchedBoardCard({
  board,
  signal,
  loading,
  compact,
  onRemove,
  onAnalyze,
}: {
  board: WatchedBoardItem
  signal?: BoardSignalSummary
  loading: boolean
  compact: boolean
  onRemove: (board: WatchedBoardItem) => void
  onAnalyze: (leader: SectorLeader) => void
}) {
  const positive = (signal?.change_5d_pct ?? 0) >= 0
  return (
    <div className={`rounded-lg border border-border/70 bg-background/60 ${compact ? 'p-3' : 'p-4'}`}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-semibold">{board.board_name}</h3>
            <span className="text-[11px] text-muted-foreground">{board.board_code}</span>
            {signal && <SignalBadge label={signal.rotation_label} tone={signal.trend_score >= 70 ? 'bg-rose-500/12 text-rose-400 border-rose-500/25' : undefined} />}
          </div>
          <p className={`mt-2 text-[12px] leading-5 text-muted-foreground ${compact ? 'max-h-10 overflow-hidden' : ''}`}>
            {loading ? '正在刷新板块 K 线和技术信号...' : signal?.summary || '暂无板块日 K 信号，请点击刷新。'}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="text-right">
            <div className={`text-[18px] font-semibold ${positive ? 'text-rose-400' : 'text-emerald-400'}`}>
              {formatPct(signal?.change_5d_pct)}
            </div>
            <div className="text-[11px] text-muted-foreground">近5日 / {signal?.asof || '--'}</div>
          </div>
          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => onRemove(board)} title="移除板块">
            <Trash2 className="w-4 h-4" />
          </Button>
        </div>
      </div>

      <div className={`mt-4 grid gap-2 ${compact ? 'grid-cols-2 xl:grid-cols-4' : 'md:grid-cols-4'}`}>
        <div className={`rounded-lg bg-accent/35 ${compact ? 'p-2.5' : 'p-3'}`}>
          <div className="text-[11px] text-muted-foreground">最新收盘</div>
          <div className="mt-1 text-[15px] font-semibold">{signal?.last_close?.toFixed(2) || '--'}</div>
        </div>
        <div className={`rounded-lg bg-accent/35 ${compact ? 'p-2.5' : 'p-3'}`}>
          <div className="text-[11px] text-muted-foreground">趋势评分</div>
          <div className="mt-1 text-[15px] font-semibold">{signal?.trend_score?.toFixed(1) || '--'}</div>
        </div>
        <div className={`rounded-lg bg-accent/35 ${compact ? 'p-2.5' : 'p-3'}`}>
          <div className="text-[11px] text-muted-foreground">MACD</div>
          <div className="mt-1"><SignalBadge label={signal?.macd_label || '--'} tone={macdTone[signal?.macd_state || '']} /></div>
        </div>
        <div className={`rounded-lg bg-accent/35 ${compact ? 'p-2.5' : 'p-3'}`}>
          <div className="text-[11px] text-muted-foreground">RSI</div>
          <div className="mt-1"><SignalBadge label={signal?.rsi_label || '--'} /></div>
        </div>
      </div>

      <div className={`mt-3 rounded-lg border border-border/60 bg-background/40 ${compact ? 'p-2' : 'p-2.5'}`}>
        <InteractiveKline
          symbol={board.board_code}
          market="CN"
          initialDays="120"
          density={compact ? 'mini' : 'compact'}
          endpointBuilder={({ symbol, days }) =>
            `/market-events/boards/${encodeURIComponent(symbol)}/kline?market=CN&days=${encodeURIComponent(String(days))}`
          }
        />
      </div>

      <div className="mt-4">
        <div className="mb-2 flex items-center gap-2 text-[12px] font-medium">
          <Brain className="w-3.5 h-3.5 text-primary" />
          龙头股深度分析
        </div>
        {signal?.leaders?.length ? (
          <div className="flex flex-wrap gap-2">
            {signal.leaders.slice(0, compact ? 3 : 5).map((leader) => (
              <Button
                key={`${leader.market}:${leader.symbol}`}
                variant="secondary"
                size="sm"
                className="h-8 px-2.5 text-[12px]"
                onClick={() => onAnalyze(leader)}
              >
                <Brain className="w-3.5 h-3.5" />
                {leader.name} {formatPct(leader.change_pct)}
              </Button>
            ))}
          </div>
        ) : (
          <div className="text-[12px] text-muted-foreground">暂无龙头股数据。</div>
        )}
      </div>
    </div>
  )
}

export default function MarketEventsPage() {
  const [market, setMarket] = useState<MarketCode>('CN')
  const [period, setPeriod] = useState<MarketEventPeriod>('week')
  const [data, setData] = useState<MarketEventsOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [watchedBoards, setWatchedBoards] = useState<WatchedBoardItem[]>([])
  const [boardSignals, setBoardSignals] = useState<Record<string, BoardSignalSummary>>({})
  const [boardsLoading, setBoardsLoading] = useState(false)
  const [boardError, setBoardError] = useState('')
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchResults, setSearchResults] = useState<BoardSearchItem[]>([])
  const [selectedLeader, setSelectedLeader] = useState<SectorLeader | null>(null)
  const [selectedEventId, setSelectedEventId] = useState('')
  const [boardViewMode, setBoardViewModeState] = useState<'compact' | 'full'>(() => {
    if (typeof window === 'undefined') return 'compact'
    return localStorage.getItem('market-events-board-view-mode') === 'full' ? 'full' : 'compact'
  })

  const setBoardViewMode = (mode: 'compact' | 'full') => {
    setBoardViewModeState(mode)
    localStorage.setItem('market-events-board-view-mode', mode)
  }

  const loadOverview = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await marketEventsApi.overview({
        market,
        period,
        event_limit: 24,
        board_limit: 12,
      })
      setData(result)
    } catch (err: any) {
      setError(err?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [market, period])

  const loadBoardSignals = useCallback(async (boards: WatchedBoardItem[]) => {
    if (!boards.length) {
      setBoardSignals({})
      return
    }
    setBoardsLoading(true)
    setBoardError('')
    try {
      const pairs = await Promise.allSettled(
        boards.map(async (board) => {
          const signal = await marketEventsApi.boardSignals(board.board_code, { market: 'CN', days: 120 })
          return [board.board_code, signal] as const
        })
      )
      const next: Record<string, BoardSignalSummary> = {}
      pairs.forEach((item) => {
        if (item.status === 'fulfilled') next[item.value[0]] = item.value[1]
      })
      setBoardSignals(next)
    } catch (err: any) {
      setBoardError(err?.message || '板块信号加载失败')
    } finally {
      setBoardsLoading(false)
    }
  }, [])

  const loadWatchlist = useCallback(async () => {
    setBoardsLoading(true)
    setBoardError('')
    try {
      const rows = await marketEventsApi.listWatchedBoards()
      setWatchedBoards(rows)
      await loadBoardSignals(rows)
    } catch (err: any) {
      setBoardError(err?.message || '关注板块加载失败')
    } finally {
      setBoardsLoading(false)
    }
  }, [loadBoardSignals])

  useEffect(() => {
    loadOverview()
  }, [loadOverview])

  useEffect(() => {
    loadWatchlist()
  }, [loadWatchlist])

  const searchBoards = async () => {
    setSearching(true)
    setBoardError('')
    try {
      const rows = await marketEventsApi.searchBoards({ q: query, limit: 20 })
      setSearchResults(rows)
    } catch (err: any) {
      setBoardError(err?.message || '搜索板块失败')
    } finally {
      setSearching(false)
    }
  }

  const addBoard = async (board: BoardSearchItem) => {
    setBoardError('')
    try {
      await marketEventsApi.addWatchedBoard({
        market: 'CN',
        board_code: board.board_code,
        board_name: board.board_name,
      })
      setQuery('')
      setSearchResults([])
      await loadWatchlist()
    } catch (err: any) {
      setBoardError(err?.message || '添加板块失败')
    }
  }

  const removeBoard = async (board: WatchedBoardItem) => {
    setBoardError('')
    try {
      await marketEventsApi.deleteWatchedBoard(board.board_code, 'CN')
      await loadWatchlist()
    } catch (err: any) {
      setBoardError(err?.message || '移除板块失败')
    }
  }

  const refreshBoards = async () => {
    setBoardsLoading(true)
    setBoardError('')
    try {
      await marketEventsApi.refreshBoards({
        market: 'CN',
        board_codes: watchedBoards.map((x) => x.board_code),
        days: 120,
      })
      await loadBoardSignals(watchedBoards)
    } catch (err: any) {
      setBoardError(err?.message || '刷新板块失败')
    } finally {
      setBoardsLoading(false)
    }
  }

  const topBoards = useMemo(() => data?.boards.slice(0, 6) || [], [data])
  const highImpactCount = useMemo(
    () => data?.events.filter((item) => item.impact_level === 'high').length || 0,
    [data]
  )
  const eventDays = useMemo(() => {
    const groups = new Map<string, MarketEventsOverview['events']>()
    ;(data?.events || []).forEach((item) => {
      const key = eventDayKey(item.event_date)
      const rows = groups.get(key) || []
      rows.push(item)
      groups.set(key, rows)
    })
    return Array.from(groups.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, events]) => ({
        date,
        events: events.slice().sort((a, b) => {
          const timeCmp = a.event_date.localeCompare(b.event_date)
          if (timeCmp !== 0) return timeCmp
          return b.impact_score - a.impact_score
        }),
      }))
  }, [data])
  const selectedEvent = useMemo(() => {
    const events = data?.events || []
    return events.find((item) => item.id === selectedEventId) || events[0] || null
  }, [data, selectedEventId])

  useEffect(() => {
    const events = data?.events || []
    if (!events.length) {
      setSelectedEventId('')
      return
    }
    if (selectedEventId && events.some((item) => item.id === selectedEventId)) return
    const preferred = events.find((item) => item.impact_level === 'high') || events[0]
    setSelectedEventId(preferred.id)
  }, [data, selectedEventId])

  return (
    <div className="space-y-5">
      <section className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
            <CalendarDays className="w-4 h-4" />
            <span>事件日历 / 板块轮动</span>
          </div>
          <h1 className="mt-2 text-2xl font-bold tracking-normal">重大事件与资金轮动</h1>
          <p className="mt-2 text-[13px] text-muted-foreground max-w-2xl">
            聚合关注池消息、公告和热门板块，给出事件影响、短线预判、资金方向和关注板块技术信号。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={market} onValueChange={(v) => setMarket(v as MarketCode)}>
            <SelectTrigger className="w-[118px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="CN">A股</SelectItem>
              <SelectItem value="HK">港股</SelectItem>
              <SelectItem value="US">美股</SelectItem>
            </SelectContent>
          </Select>
          <Select value={period} onValueChange={(v) => setPeriod(v as MarketEventPeriod)}>
            <SelectTrigger className="w-[128px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="week">本周</SelectItem>
              <SelectItem value="month">本月</SelectItem>
              <SelectItem value="rolling_month">近30天</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="secondary" onClick={loadOverview} disabled={loading}>
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </section>

      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
          {error}
        </div>
      )}

      <section className="grid gap-3 md:grid-cols-3">
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-muted-foreground">统计周期</span>
            <Clock3 className="w-4 h-4 text-primary" />
          </div>
          <div className="mt-3 text-xl font-semibold">{periodLabel[period]}</div>
          <div className="mt-1 text-[12px] text-muted-foreground">起始 {data?.start_date || '--'} / {marketLabel[market]}</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-muted-foreground">重大事件</span>
            <Newspaper className="w-4 h-4 text-amber-400" />
          </div>
          <div className="mt-3 text-xl font-semibold">{loading ? '--' : highImpactCount}</div>
          <div className="mt-1 text-[12px] text-muted-foreground">高影响事件 / 新闻样本 {data?.coverage.news_items ?? '--'}</div>
        </div>
        <div className="card p-4">
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-muted-foreground">轮动主线</span>
            <Flame className="w-4 h-4 text-rose-400" />
          </div>
          <div className="mt-3 text-xl font-semibold truncate">{data?.rotation.hot_boards[0] || '--'}</div>
          <div className="mt-1 text-[12px] text-muted-foreground">覆盖板块 {data?.coverage.board_count ?? '--'}</div>
        </div>
      </section>

      <section className="card p-4 md:p-5">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <h2 className="text-[16px] font-semibold flex items-center gap-2">
              <BarChart3 className="w-4 h-4 text-primary" />
              关注板块池
            </h2>
            <p className="mt-1 text-[12px] text-muted-foreground">选择最多 8 个 A 股行业板块，刷新最近 120 个交易日的日 K、MACD 和 RSI 信号。</p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="inline-flex h-10 rounded-lg border border-border/70 bg-accent/30 p-1">
              <button
                type="button"
                onClick={() => setBoardViewMode('compact')}
                className={`inline-flex items-center gap-1.5 rounded-md px-2.5 text-[12px] transition-colors ${
                  boardViewMode === 'compact' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                <Layers className="w-3.5 h-3.5" />
                紧凑
              </button>
              <button
                type="button"
                onClick={() => setBoardViewMode('full')}
                className={`inline-flex items-center gap-1.5 rounded-md px-2.5 text-[12px] transition-colors ${
                  boardViewMode === 'full' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                <BarChart3 className="w-3.5 h-3.5" />
                完整
              </button>
            </div>
            <div className="relative w-full sm:w-[280px]">
              <Search className="absolute left-2.5 top-2.5 w-4 h-4 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') searchBoards()
                }}
                placeholder="搜索板块名称或代码"
                className="pl-8"
              />
            </div>
            <Button variant="secondary" onClick={searchBoards} disabled={searching}>
              {searching ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
              搜索
            </Button>
            <Button onClick={refreshBoards} disabled={boardsLoading || watchedBoards.length === 0}>
              <RefreshCw className={`w-4 h-4 ${boardsLoading ? 'animate-spin' : ''}`} />
              更新日K
            </Button>
          </div>
        </div>

        {boardError && (
          <div className="mt-4 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
            {boardError}
          </div>
        )}

        {searchResults.length > 0 && (
          <div className="mt-4 rounded-lg border border-border/70 bg-background/60 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[12px] text-muted-foreground">搜索结果</span>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setSearchResults([])}>
                <X className="w-4 h-4" />
              </Button>
            </div>
            <div className="flex flex-wrap gap-2">
              {searchResults.map((item) => {
                const selected = watchedBoards.some((x) => x.board_code === item.board_code)
                return (
                  <Button
                    key={item.board_code}
                    variant={selected ? 'secondary' : 'outline'}
                    size="sm"
                    className="h-8 px-2.5 text-[12px]"
                    disabled={selected}
                    onClick={() => addBoard(item)}
                  >
                    {item.board_name} {formatPct(item.change_pct)}
                  </Button>
                )
              })}
            </div>
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          {watchedBoards.map((board) => (
            <span key={board.board_code} className="inline-flex items-center gap-1.5 rounded-md border border-border/70 bg-accent/40 px-2.5 py-1 text-[12px]">
              {board.board_name}
              <button className="text-muted-foreground hover:text-foreground" onClick={() => removeBoard(board)} type="button">
                <X className="w-3.5 h-3.5" />
              </button>
            </span>
          ))}
          {!watchedBoards.length && <span className="text-[12px] text-muted-foreground">还没有关注板块，可以先搜索“半导体”“证券”“新能源”等。</span>}
        </div>

        <div className="mt-5 grid gap-3 xl:grid-cols-2">
          {boardsLoading && !watchedBoards.length ? (
            <div className="h-32 rounded-lg bg-muted/50 animate-pulse" />
          ) : watchedBoards.length === 0 ? (
            <EmptyState text="选择几个板块后，这里会展示板块日 K、MACD、RSI 和轮动判断。" />
          ) : (
            watchedBoards.map((board) => (
              <WatchedBoardCard
                key={board.board_code}
                board={board}
                signal={boardSignals[board.board_code]}
                loading={boardsLoading}
                compact={boardViewMode === 'compact'}
                onRemove={removeBoard}
                onAnalyze={setSelectedLeader}
              />
            ))
          )}
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.08fr)_minmax(360px,0.92fr)]">
        <div className="card p-4 md:p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-[16px] font-semibold flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 text-amber-400" />
                {periodLabel[period]}重大事件
              </h2>
              <p className="mt-1 text-[12px] text-muted-foreground">按日期聚合宏观日历、政策窗口和关注池消息</p>
            </div>
            <span className="text-[12px] text-muted-foreground">{data?.events.length || 0} 条</span>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
            {loading ? (
              <EventSkeleton />
            ) : !data?.events.length ? (
              <EmptyState text="当前周期暂无可用重大事件。" />
            ) : (
              <>
                <div className="space-y-3">
                  {eventDays.map((day) => (
                    <div key={day.date} className="rounded-lg border border-border/60 bg-background/45 p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <CalendarDays className="w-4 h-4 text-primary" />
                          <span className="text-[13px] font-semibold">{dateLabel(day.date)}</span>
                        </div>
                        <span className="text-[11px] text-muted-foreground">{day.events.length} 项</span>
                      </div>
                      <div className="space-y-2">
                        {day.events.map((item) => {
                          const active = selectedEvent?.id === item.id
                          return (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => setSelectedEventId(item.id)}
                              className={`w-full rounded-lg border p-3 text-left transition-colors ${
                                active
                                  ? 'border-primary/50 bg-primary/8'
                                  : 'border-border/50 bg-accent/20 hover:border-primary/30 hover:bg-accent/35'
                              }`}
                            >
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="text-[11px] font-mono text-muted-foreground">{eventTimeLabel(item.event_date)}</span>
                                <span className={`px-1.5 py-0.5 rounded-md border text-[10.5px] ${impactClass(item.impact_level)}`}>
                                  {impactLabel[item.impact_level]}
                                </span>
                                <span className={`px-1.5 py-0.5 rounded-md border text-[10.5px] ${sentimentClass(item.sentiment)}`}>
                                  {sentimentLabel[item.sentiment]}
                                </span>
                                <span className="text-[10.5px] text-muted-foreground">{item.event_category || item.source_label}</span>
                              </div>
                              <div className="mt-2 text-[13px] font-medium leading-5 text-foreground">{item.title}</div>
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {item.related_boards.slice(0, 4).map((board) => (
                                  <span key={board} className="px-1.5 py-0.5 rounded-md bg-primary/10 text-primary text-[10.5px]">
                                    {board}
                                  </span>
                                ))}
                              </div>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  ))}
                </div>

                <aside className="rounded-lg border border-border/60 bg-background/55 p-4 lg:sticky lg:top-24 lg:self-start">
                  {selectedEvent ? (
                    <>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`px-2 py-1 rounded-md border text-[11px] ${impactClass(selectedEvent.impact_level)}`}>
                          {impactLabel[selectedEvent.impact_level]} {selectedEvent.impact_score.toFixed(1)}
                        </span>
                        <span className={`px-2 py-1 rounded-md border text-[11px] ${sentimentClass(selectedEvent.sentiment)}`}>
                          {sentimentLabel[selectedEvent.sentiment]}
                        </span>
                      </div>
                      <div className="mt-3 text-[11px] text-muted-foreground">
                        {selectedEvent.event_date} / {selectedEvent.event_category || selectedEvent.source_label}
                      </div>
                      <h3 className="mt-2 text-[15px] font-semibold leading-6">{selectedEvent.title}</h3>
                      <div className="mt-3 rounded-lg bg-accent/30 p-3">
                        <div className="flex items-center gap-2 text-[12px] font-medium">
                          <Zap className="w-3.5 h-3.5 text-primary" />
                          AI 解读结论
                        </div>
                        <p className="mt-2 text-[12px] leading-5 text-foreground/90">
                          {selectedEvent.ai_conclusion || selectedEvent.impact_summary}
                        </p>
                      </div>
                      <p className="mt-3 text-[12px] leading-5 text-muted-foreground">{selectedEvent.prediction}</p>
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {selectedEvent.related_boards.map((board) => (
                          <span key={board} className="px-2 py-1 rounded-md bg-primary/10 text-primary text-[11px]">
                            {board}
                          </span>
                        ))}
                        {selectedEvent.symbols.slice(0, 5).map((symbol) => (
                          <span key={symbol} className="px-2 py-1 rounded-md bg-accent/50 text-muted-foreground text-[11px]">
                            {symbol}
                          </span>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div className="text-[12px] text-muted-foreground">选择左侧事件查看解读。</div>
                  )}
                </aside>
              </>
            )}
          </div>
        </div>

        <aside className="space-y-5">
          <div className="card p-4 md:p-5">
            <h2 className="text-[16px] font-semibold flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-rose-400" />
              资金轮动判断
            </h2>
            <p className="mt-3 text-[13px] leading-6 text-muted-foreground">
              {loading ? '正在计算板块强弱和消息面主题...' : data?.rotation.summary}
            </p>
            <div className="mt-4 space-y-2">
              {(data?.rotation.watch_points || []).map((point) => (
                <div key={point} className="flex gap-2 text-[12px] leading-5 text-muted-foreground">
                  <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-primary shrink-0" />
                  <span>{point}</span>
                </div>
              ))}
            </div>
            <div className="mt-4 flex flex-wrap gap-1.5">
              {(data?.rotation.event_topics || []).map((topic) => (
                <span key={topic} className="px-2 py-1 rounded-md bg-accent/60 text-[11px] text-muted-foreground">
                  {topic}
                </span>
              ))}
            </div>
          </div>

          <div className="card p-4 md:p-5">
            <div className="flex items-center justify-between">
              <h2 className="text-[16px] font-semibold flex items-center gap-2">
                <Layers className="w-4 h-4 text-primary" />
                热门板块走势
              </h2>
              <span className="text-[12px] text-muted-foreground">热度排序</span>
            </div>
            <div className="mt-4 space-y-3">
              {loading ? (
                Array.from({ length: 4 }).map((_, index) => (
                  <div key={index} className="h-28 rounded-lg bg-muted/50 animate-pulse" />
                ))
              ) : topBoards.length === 0 ? (
                <EmptyState text="暂无可用板块走势数据。" />
              ) : (
                topBoards.map((board, index) => (
                  <BoardRow key={board.code} board={board} index={index} />
                ))
              )}
            </div>
          </div>
        </aside>
      </section>

      {selectedLeader && (
        <DeepAnalysisModal
          open={!!selectedLeader}
          onOpenChange={(open) => {
            if (!open) setSelectedLeader(null)
          }}
          stockId={0}
          stockName={selectedLeader.name}
          stockSymbol={selectedLeader.symbol}
          stockMarket={selectedLeader.market || 'CN'}
        />
      )}
    </div>
  )
}
