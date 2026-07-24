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

export type SectorCategoryKey =
  | 'finance'
  | 'resource'
  | 'energy'
  | 'channel'
  | 'consumer'
  | 'theme'
  | 'other'

export type BoardTier = 'pool' | 'pinned'
export type BoardScope = 'industry' | 'concept'
export type BoardEventType = 'policy' | 'industry' | 'earnings' | 'macro' | 'case'

export interface WatchedBoardItem {
  id: number
  market: 'CN'
  board_code: string
  board_name: string
  sort_order: number
  enabled: boolean
  category: SectorCategoryKey | ''
  tier: BoardTier
  scope: BoardScope
  tags: string[]
  created_at: string | null
  updated_at: string | null
}

export type ValuationLabel = 'low' | 'fair' | 'high' | 'unknown'

export interface BoardValuationCompact {
  pe: number
  pe_percentile_3y: number | null
  label: ValuationLabel
  history_days: number
}

export interface BoardValuationDetail {
  board_code: string
  available: boolean
  reason?: string
  sw_code?: string
  sw_name?: string
  date?: string
  pe?: number | null
  pb?: number | null
  dividend_yield?: number | null
  history_days?: number
  pe_percentile?: { '3y': number | null; '5y': number | null }
  pb_percentile?: { '3y': number | null; '5y': number | null }
  label?: ValuationLabel
}

export interface PoolBoardItem extends WatchedBoardItem {
  change_pct: number | null
  turnover: number | null
  leader_name?: string | null
  valuation?: BoardValuationCompact | null
}

export interface BoardPoolResponse {
  market: 'CN'
  categories: Array<{
    key: SectorCategoryKey
    label: string
    boards: PoolBoardItem[]
  }>
  board_count: number
  seed_report: {
    created: number
    updated: number
    unresolved: string[]
    total_seed: number
  } | null
}

export interface BoardEventMarkItem {
  id: number
  market: 'CN'
  board_code: string
  date: string
  event_type: BoardEventType
  title: string
  summary: string
  importance: number
  source: 'manual' | 'auto'
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

  boardKline: (boardCode: string, params?: { market?: 'CN'; days?: number; interval?: '1d' | '1w' }) =>
    fetchAPI<BoardKlineResponse>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/kline`, {
        market: params?.market || 'CN',
        days: params?.days || 120,
        interval: params?.interval,
      }),
      { timeoutMs: 30000 }
    ),

  boardPool: (params?: { market?: 'CN'; auto_seed?: boolean }) =>
    fetchAPI<BoardPoolResponse>(
      withQuery('/market-events/boards/pool', {
        market: params?.market,
        auto_seed: params?.auto_seed,
      }),
      { timeoutMs: 60000 }
    ),

  reseedBoardPool: () =>
    fetchAPI<BoardPoolResponse['seed_report']>('/market-events/boards/pool/seed', {
      method: 'POST',
      timeoutMs: 60000,
    }),

  boardValuation: (boardCode: string, market: 'CN' = 'CN') =>
    fetchAPI<BoardValuationDetail>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/valuation`, { market }),
      { timeoutMs: 30000 }
    ),

  valuationStatus: () =>
    fetchAPI<{ rows: number; industries: number; min_date: string | null; max_date: string | null }>(
      '/market-events/boards/valuation/status',
      { timeoutMs: 30000 }
    ),

  addPoolBoard: (payload: {
    market?: 'CN'
    board_code: string
    board_name: string
    category?: SectorCategoryKey | ''
    scope?: BoardScope
  }) =>
    fetchAPI<WatchedBoardItem>('/market-events/boards/pool', {
      method: 'POST',
      body: JSON.stringify({ market: payload.market || 'CN', ...payload }),
      timeoutMs: 30000,
    }),

  updatePoolBoard: (
    boardCode: string,
    payload: { market?: 'CN'; category?: SectorCategoryKey | ''; tier?: BoardTier; enabled?: boolean }
  ) =>
    fetchAPI<WatchedBoardItem>(`/market-events/boards/pool/${encodeURIComponent(boardCode)}`, {
      method: 'PATCH',
      body: JSON.stringify({ market: payload.market || 'CN', ...payload }),
      timeoutMs: 30000,
    }),

  listBoardEvents: (boardCode: string, market: 'CN' = 'CN') =>
    fetchAPI<BoardEventMarkItem[]>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/events`, { market }),
      { timeoutMs: 30000 }
    ),

  createBoardEvent: (
    boardCode: string,
    payload: {
      market?: 'CN'
      date: string
      event_type: BoardEventType
      title: string
      summary?: string
      importance?: number
    }
  ) =>
    fetchAPI<BoardEventMarkItem>(`/market-events/boards/${encodeURIComponent(boardCode)}/events`, {
      method: 'POST',
      body: JSON.stringify({ market: payload.market || 'CN', ...payload }),
      timeoutMs: 30000,
    }),

  deleteBoardEvent: (markId: number) =>
    fetchAPI<{ ok: boolean }>(`/market-events/boards/events/${markId}`, {
      method: 'DELETE',
      timeoutMs: 30000,
    }),

  boardSignals: (boardCode: string, params?: { market?: 'CN'; days?: number }) =>
    fetchAPI<BoardSignalSummary>(
      withQuery(`/market-events/boards/${encodeURIComponent(boardCode)}/signals`, {
        market: params?.market || 'CN',
        days: params?.days || 120,
      }),
      { timeoutMs: 30000 }
    ),
}
