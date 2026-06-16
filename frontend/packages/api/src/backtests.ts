import { fetchAPI } from './client'

export interface BacktestRun {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | string
  message: string
  market: string
  start_date: string
  end_date: string
  initial_capital: number
  strategy_codes: string[]
  params: Record<string, any>
  summary: Record<string, any>
  error?: string
  created_at?: string
  started_at?: string
  finished_at?: string
  updated_at?: string
}

export interface BacktestTrade {
  id: number
  strategy_code: string
  strategy_name: string
  strategy_type: string
  stock_symbol: string
  stock_market: string
  stock_name: string
  quantity: number
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  stop_loss?: number | null
  target_price?: number | null
  pnl: number
  pnl_pct: number
  fees: number
  holding_days: number
  exit_reason: string
  skipped: boolean
  skip_reason: string
  meta: Record<string, any>
}

export interface BacktestEquityPoint {
  date: string
  cash: number
  positions_value: number
  equity: number
  drawdown_pct: number
}

export interface BacktestStrategyMetric {
  strategy_code: string
  strategy_name: string
  strategy_type: string
  stock_market: string
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  total_return_pct: number
  avg_return_pct: number
  max_drawdown_pct: number
  recent_30d_return_pct: number
  sample_size: number
  exit_reason_counts: Record<string, number>
  skip_reason_counts: Record<string, number>
}

export interface CreateBacktestRunInput {
  strategy_codes?: string[]
  market?: string
  start_date?: string
  end_date?: string
  initial_capital?: number
  params?: Record<string, any>
}

export const backtestsApi = {
  createRun: (payload: CreateBacktestRunInput) =>
    fetchAPI<BacktestRun>('/backtests/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }),

  getRun: (id: string) =>
    fetchAPI<BacktestRun>(`/backtests/runs/${encodeURIComponent(id)}`),

  getTrades: (id: string, params?: { limit?: number; offset?: number; include_skipped?: boolean }) => {
    const search = new URLSearchParams()
    if (params?.limit) search.set('limit', String(params.limit))
    if (params?.offset) search.set('offset', String(params.offset))
    if (params?.include_skipped != null) search.set('include_skipped', params.include_skipped ? 'true' : 'false')
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return fetchAPI<{ items: BacktestTrade[]; total: number; limit: number; offset: number }>(
      `/backtests/runs/${encodeURIComponent(id)}/trades${suffix}`
    )
  },

  getEquity: (id: string) =>
    fetchAPI<{ items: BacktestEquityPoint[] }>(`/backtests/runs/${encodeURIComponent(id)}/equity`),

  getStrategies: (id: string) =>
    fetchAPI<{ items: BacktestStrategyMetric[] }>(`/backtests/runs/${encodeURIComponent(id)}/strategies`),
}
