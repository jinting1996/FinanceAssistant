import { fetchAPI } from './client'

export interface StrategyPromptItem {
  id: number
  name: string
  description: string
  prompt: string
  is_default: boolean
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface StrategyTags {
  prev_high?: number | null
  breakout?: 'valid' | 'pending' | 'failed' | 'expired' | 'none' | string
  gap_to_prev_high_pct?: number | null
  support?: number | null
  pullback_support?: boolean
  volume_confirm?: 'strong' | 'weak' | 'neutral' | 'none' | string
  score?: number | null
  status?: string | null
  event_age?: number | null
  d0_vol_ratio?: number | null
  blue_chip?: boolean | null
  action?: string
  action_label?: string
  reason?: string
}

export interface StrategyPoolItem {
  id: number
  symbol: string
  market: string
  name: string
  source: string
  note: string
  tags?: StrategyTags
  tags_updated_at?: string
  created_at: string
}

export interface StrategyAnalysisResultItem {
  id: number
  strategy_id: number | null
  strategy_name: string
  symbol: string
  market: string
  name: string
  verdict: string
  content: string
  model: string
  created_at: string
}

export interface StrategyPromptPayload {
  name: string
  description?: string
  prompt: string
  enabled?: boolean
}

export interface StrategyAnalyzePayload {
  strategy_id: number
  symbol: string
  market?: string
  name?: string
  kline_days?: number
}

export const strategyAnalysisApi = {
  listStrategies: () =>
    fetchAPI<{ items: StrategyPromptItem[] }>('/strategy-analysis/strategies'),
  createStrategy: (payload: StrategyPromptPayload) =>
    fetchAPI<StrategyPromptItem>('/strategy-analysis/strategies', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateStrategy: (id: number, payload: StrategyPromptPayload) =>
    fetchAPI<StrategyPromptItem>(`/strategy-analysis/strategies/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteStrategy: (id: number) =>
    fetchAPI<{ ok: boolean }>(`/strategy-analysis/strategies/${id}`, { method: 'DELETE' }),
  // 清空全部策略（含默认），下次拉取会重灌一条空白模板
  clearStrategies: () =>
    fetchAPI<{ ok: boolean; deleted: number }>('/strategy-analysis/strategies', { method: 'DELETE' }),

  listPool: () => fetchAPI<{ items: StrategyPoolItem[] }>('/strategy-analysis/pool'),
  addPoolItem: (payload: { symbol: string; market?: string; name?: string; note?: string }) =>
    fetchAPI<{ created: number }>('/strategy-analysis/pool', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  importPositions: () =>
    fetchAPI<{ created: number; scanned: number }>('/strategy-analysis/pool/import-positions', {
      method: 'POST',
    }),
  deletePoolItem: (id: number) =>
    fetchAPI<{ ok: boolean }>(`/strategy-analysis/pool/${id}`, { method: 'DELETE' }),

  analyze: (payload: StrategyAnalyzePayload) =>
    fetchAPI<StrategyAnalysisResultItem>('/strategy-analysis/analyze', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 120000,
    }),
  // 一键重测：对策略池内所有股票用当前策略批量无头分析，回写徽章
  reanalyzeAll: (strategyId: number) =>
    fetchAPI<StrategyReanalyzeResult>('/strategy-analysis/reanalyze-all', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId }),
      timeoutMs: 300000,
    }),
  listResults: (strategyId?: number) =>
    fetchAPI<{ items: StrategyAnalysisResultItem[] }>(
      `/strategy-analysis/results${strategyId ? `?strategy_id=${strategyId}` : ''}`,
    ),
  lastConversations: (strategyId: number) =>
    fetchAPI<{ items: Record<string, { conversation_id: number; updated_at: string; title: string }> }>(
      `/strategy-analysis/last-conversations?strategy_id=${strategyId}`,
    ),
  // 读取缓存的排序快照（不触发 AI）
  getOverview: (strategyId: number) =>
    fetchAPI<StrategyOverview>(`/strategy-analysis/overview?strategy_id=${strategyId}`),
  // 重新计算排序（触发 AI）并落缓存
  overview: (strategyId: number) =>
    fetchAPI<StrategyOverview>('/strategy-analysis/overview', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId }),
      timeoutMs: 120000,
    }),
}

export interface StrategyReanalyzeResult {
  total: number
  analyzed: number
  failed: Array<{ symbol: string; error: string }>
  model: string
  analyzed_at: string
}

export interface StrategyOverviewRow {
  rank: number
  symbol: string
  market: string
  name: string
  score: number | null
  reason: string
  tags: StrategyTags
  tags_updated_at?: string
}

export interface StrategyOverview {
  summary: string
  ranked: StrategyOverviewRow[]
  // 已分析但无总分（未达可评分状态）→ 不参与排名，注明原因
  excluded?: Array<{ symbol: string; market: string; name: string; reason: string }>
  unanalyzed: Array<{ symbol: string; market: string; name: string }>
  model: string
  analyzed_at: string
  // 排序后有票被重新分析过 / 池子有增减 → 快照已过期，建议刷新排序
  stale?: boolean
}
