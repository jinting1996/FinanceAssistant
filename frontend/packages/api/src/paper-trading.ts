import { fetchAPI } from './client'

export interface PaperTradingAccountResponse {
  id: number
  initial_capital: number
  current_capital: number
  total_equity: number
  total_pnl: number
  unrealized_pnl: number
  total_trades: number
  winning_trades: number
  win_rate: number
  max_drawdown_pct: number
  peak_capital: number
  enabled: boolean
  excluded_markets: string[]
  /** 各市场投资比例 {CN/HK/US: 0~1} */
  market_allocations: Record<string, number>
  /** 仅按单市场口径返回时存在 */
  market?: string
  allocation_ratio?: number
  created_at: string
  updated_at: string
}

export type MarketView = 'ALL' | 'CN' | 'HK' | 'US'

export interface PaperTradingPositionItem {
  id: number
  stock_symbol: string
  stock_market: string
  stock_name: string
  quantity: number
  entry_price: number
  stop_loss?: number | null
  target_price?: number | null
  current_price?: number | null
  unrealized_pnl: number
  unrealized_pnl_pct: number
  status: string
  signal_run_id?: number | null
  signal_snapshot_date: string
  signal_action: string
  strategy_code: string
  holding_days: number
  opened_at: string
  closed_at: string
  updated_at: string
}

export interface PaperTradingTradeItem {
  id: number
  stock_symbol: string
  stock_market: string
  stock_name: string
  quantity: number
  entry_price: number
  exit_price: number
  pnl: number
  pnl_pct: number
  exit_reason: string
  signal_run_id?: number | null
  signal_snapshot_date: string
  strategy_code: string
  holding_days: number
  opened_at: string
  closed_at: string
}

export interface PaperTradingTradesResponse {
  total: number
  items: PaperTradingTradeItem[]
}

export interface EquityCurvePoint {
  date: string
  equity: number
}

export interface StrategyPerformanceItem {
  strategy_code: string
  total_trades: number
  winning_trades: number
  win_rate: number
  total_pnl: number
  avg_pnl_pct: number
  avg_holding_days: number
  open_positions: number
  unrealized_pnl: number
  skipped_count?: number
  exit_reason_counts?: Record<string, number>
}

export interface PaperTradingMetricsResponse {
  account: PaperTradingAccountResponse | null
  equity_curve: EquityCurvePoint[]
  open_positions: number
  strategy_performance: StrategyPerformanceItem[]
  skip_stats?: {
    total: number
    by_reason: Record<string, number>
    by_strategy: Record<string, number>
    samples: Array<Record<string, any>>
    updated_at?: string
  }
}

export interface NotifyChannelItem {
  id: number
  name: string
  type: string
  is_default: boolean
}

export interface PaperTradingNotifySettings {
  settings: {
    pt_notify_enabled: string
    pt_notify_channel_ids: string
    pt_notify_realtime: string
    pt_notify_premarket: string
    pt_notify_summary: string
  }
  channels: NotifyChannelItem[]
}

export interface PaperTradingScreenerStrategyResponse {
  ok: boolean
  run_id: number
  strategy_code: string
  strategy_name: string
  created: number
  updated: number
  skipped: number
  scan?: { status: string; opened?: number; closed?: number } | null
}

export interface PaperTradingStrategySelection {
  mode: 'all' | 'custom' | 'top_n'
  strategy_codes: string[]
  top_n: number
}

export interface PaperTradingStrategySelectionResponse {
  selection: PaperTradingStrategySelection
  strategy_pool: Array<{
    code: string
    name: string
    enabled: boolean
    strategy_type?: string
    ranking?: Record<string, any>
  }>
}

export const paperTradingApi = {
  getAccount: (market?: string) =>
    fetchAPI<PaperTradingAccountResponse>(
      `/paper-trading/account${market && market !== 'ALL' ? `?market=${encodeURIComponent(market)}` : ''}`
    ),

  listPositions: (status = 'open', market?: string) =>
    fetchAPI<PaperTradingPositionItem[]>(
      `/paper-trading/positions?status=${encodeURIComponent(status)}${market && market !== 'ALL' ? `&market=${encodeURIComponent(market)}` : ''}`
    ),

  listTrades: (limit = 50, offset = 0, market?: string) =>
    fetchAPI<PaperTradingTradesResponse>(
      `/paper-trading/trades?limit=${encodeURIComponent(String(limit))}&offset=${encodeURIComponent(String(offset))}${market && market !== 'ALL' ? `&market=${encodeURIComponent(market)}` : ''}`
    ),

  getMetrics: (market?: string, strategyCode?: string) => {
    const params = new URLSearchParams()
    if (market && market !== 'ALL') params.set('market', market)
    if (strategyCode) params.set('strategy_code', strategyCode)
    const qs = params.toString()
    return fetchAPI<PaperTradingMetricsResponse>(`/paper-trading/metrics${qs ? `?${qs}` : ''}`)
  },

  toggleAccount: (enabled: boolean) =>
    fetchAPI<PaperTradingAccountResponse>('/paper-trading/account/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  resetAccount: () =>
    fetchAPI<{ ok: boolean }>('/paper-trading/account/reset', {
      method: 'POST',
    }),

  closePosition: (positionId: number) =>
    fetchAPI<{ ok: boolean }>(`/paper-trading/positions/${encodeURIComponent(String(positionId))}/close`, {
      method: 'POST',
    }),

  updateSettings: (settings: {
    excluded_markets?: string[]
    market_allocations?: Record<string, number>
    initial_capital?: number
  }) =>
    fetchAPI<PaperTradingAccountResponse>('/paper-trading/account/settings', {
      method: 'POST',
      body: JSON.stringify(settings),
    }),

  scan: () =>
    fetchAPI<{ status: string; opened: number; closed: number; skipped?: number; skip_stats?: PaperTradingMetricsResponse['skip_stats'] }>('/paper-trading/scan', {
      method: 'POST',
      timeoutMs: 30000,
    }),

  createScreenerStrategy: (payload: {
    run_id?: number
    formula_id?: number
    max_results?: number
    min_change_pct?: number | null
    trigger_scan?: boolean
  }) =>
    fetchAPI<PaperTradingScreenerStrategyResponse>('/paper-trading/screener-strategy', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }),

  getStrategySelection: () =>
    fetchAPI<PaperTradingStrategySelectionResponse>('/paper-trading/strategy-selection'),

  updateStrategySelection: (payload: PaperTradingStrategySelection) =>
    fetchAPI<PaperTradingStrategySelectionResponse>('/paper-trading/strategy-selection', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getNotifySettings: () =>
    fetchAPI<PaperTradingNotifySettings>('/paper-trading/notify-settings'),

  updateNotifySettings: (settings: Record<string, string>) =>
    fetchAPI<PaperTradingNotifySettings>('/paper-trading/notify-settings', {
      method: 'POST',
      body: JSON.stringify(settings),
    }),

  testNotify: () =>
    fetchAPI<{ ok: boolean }>('/paper-trading/notify-test', {
      method: 'POST',
    }),
}
