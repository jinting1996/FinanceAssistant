import { fetchAPI } from './client'

type QueryValue = string | number | boolean | null | undefined

function withQuery(path: string, params: Record<string, QueryValue>): string {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return
    const sv = String(v).trim()
    if (!sv) return
    q.set(k, sv)
  })
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

export type MarketEventPeriod = 'week' | 'month' | 'rolling_month'
export type MarketCode = 'CN' | 'HK' | 'US'
export type EventSentiment = 'positive' | 'negative' | 'neutral'
export type ImpactLevel = 'high' | 'medium' | 'low'
export type FlowState = 'inflow' | 'active' | 'cooling' | 'neutral'

export interface MarketEventItem {
  id: string
  title: string
  content: string
  source: string
  source_label: string
  event_category?: string
  event_date: string
  symbols: string[]
  importance: number
  sentiment: EventSentiment
  impact_level: ImpactLevel
  impact_score: number
  impact_summary: string
  prediction: string
  ai_conclusion?: string
  related_boards: string[]
  url: string
}

export interface SectorLeader {
  symbol: string
  market: string
  name: string
  price: number | null
  change_pct: number | null
  turnover: number | null
}

export interface SectorFlowItem {
  code: string
  name: string
  change_pct: number | null
  turnover: number | null
  rank_gainers: number | null
  rank_turnover: number | null
  flow_score: number
  flow_state: FlowState
  flow_label: string
  rotation_signal: string
  leaders: SectorLeader[]
}

export interface RotationSummary {
  summary: string
  hot_boards: string[]
  cooling_boards: string[]
  event_topics: string[]
  watch_points: string[]
}

export interface MarketEventsOverview {
  market: MarketCode
  period: MarketEventPeriod
  start_date: string
  generated_at: string
  events: MarketEventItem[]
  boards: SectorFlowItem[]
  rotation: RotationSummary
  coverage: {
    watchlist_symbols: number
    news_items: number
    newsnow_items?: number
    board_count: number
  }
}

export interface WatchedBoardItem {
  id: number
  market: 'CN'
  board_code: string
  board_name: string
  sort_order: number
  enabled: boolean
  created_at: string | null
  updated_at: string | null
}

export interface BoardSearchItem {
  market: 'CN'
  board_code: string
  board_name: string
  change_pct: number | null
  turnover: number | null
}

export interface BoardKlineItem {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number | null
  turnover: number | null
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi6?: number | null
  rsi12?: number | null
}

export interface BoardKlineResponse {
  symbol: string
  market: 'CN'
  days: number
  interval: '1d'
  klines: BoardKlineItem[]
}

export interface BoardSignalSummary {
  market: 'CN'
  board_code: string
  board_name: string
  days: number
  available: boolean
  asof: string | null
  last_close: number | null
  change_1d_pct: number | null
  change_5d_pct: number | null
  change_20d_pct: number | null
  macd_state: string
  macd_label: string
  rsi_state: string
  rsi_label: string
  rsi6?: number | null
  rsi12?: number | null
  trend_score: number
  rotation_state: string
  rotation_label: string
  summary: string
  series: BoardKlineItem[]
  leaders: SectorLeader[]
}

export interface BoardRefreshResult {
  market: 'CN'
  days: number
  count: number
  updated: number
  results: Array<{
    board_code: string
    updated: number
    ok: boolean
    error?: string
  }>
}

export const marketEventsApi = {
  overview: (params?: {
    market?: MarketCode
    period?: MarketEventPeriod
    event_limit?: number
    board_limit?: number
  }) =>
    fetchAPI<MarketEventsOverview>(
      withQuery('/market-events/overview', {
        market: params?.market,
        period: params?.period,
        event_limit: params?.event_limit,
        board_limit: params?.board_limit,
      }),
      { timeoutMs: 30000 }
    ),

  searchBoards: (params?: { q?: string; limit?: number }) =>
    fetchAPI<BoardSearchItem[]>(
      withQuery('/market-events/boards/search', {
        q: params?.q,
        limit: params?.limit,
      }),
      { timeoutMs: 30000 }
    ),

  listWatchedBoards: () =>
    fetchAPI<WatchedBoardItem[]>('/market-events/boards/watchlist', { timeoutMs: 30000 }),

  addWatchedBoard: (payload: { market?: 'CN'; board_code: string; board_name: string }) =>
    fetchAPI<WatchedBoardItem>('/market-events/boards/watchlist', {
      method: 'POST',
      body: JSON.stringify({
        market: payload.market || 'CN',
        board_code: payload.board_code,
        board_name: payload.board_name,
      }),
      timeoutMs: 30000,
    }),

  deleteWatchedBoard: (boardCode: string, market: 'CN' = 'CN') =>
    fetchAPI<{ ok: boolean }>(
      withQuery(`/market-events/boards/watchlist/${encodeURIComponent(boardCode)}`, { market }),
      { method: 'DELETE', timeoutMs: 30000 }
    ),

  refreshBoards: (payload?: { market?: 'CN'; board_codes?: string[]; days?: number }) =>
    fetchAPI<BoardRefreshResult>('/market-events/boards/refresh', {
      method: 'POST',
      body: JSON.stringify({
        market: payload?.market || 'CN',
        board_codes: payload?.board_codes,
        days: payload?.days || 120,
      }),
      timeoutMs: 60000,
    }),

  boardKline: (boardCode: string, params?: { market?: 'CN'; days?: number }) =>
    fetchAPI<BoardKlineResponse>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/kline`, {
        market: params?.market || 'CN',
        days: params?.days || 120,
      }),
      { timeoutMs: 30000 }
    ),

  boardSignals: (boardCode: string, params?: { market?: 'CN'; days?: number }) =>
    fetchAPI<BoardSignalSummary>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/signals`, {
        market: params?.market || 'CN',
        days: params?.days || 120,
      }),
      { timeoutMs: 30000 }
    ),
}
