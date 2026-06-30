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

export interface StrategyPoolItem {
  id: number
  symbol: string
  market: string
  name: string
  source: string
  note: string
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
  listResults: (strategyId?: number) =>
    fetchAPI<{ items: StrategyAnalysisResultItem[] }>(
      `/strategy-analysis/results${strategyId ? `?strategy_id=${strategyId}` : ''}`,
    ),
}
