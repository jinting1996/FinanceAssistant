import { fetchAPI } from './client'

export interface ScreenerFormulaItem {
  id: number
  name: string
  description: string
  formula: string
  universe_config: Record<string, any>
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ScreenerResultItem {
  run_id: number
  symbol: string
  market: string
  name: string
  board_code: string
  board_name: string
  last_close: number | null
  change_pct: number | null
  matched: boolean
  reason: string
  indicators: Record<string, any>
}

export interface ScreenerRunItem {
  id: number
  formula_id: number | null
  formula_snapshot: string
  universe_config: Record<string, any>
  status: 'queued' | 'running' | 'success' | 'failed' | 'cancelled' | 'stale'
  task_id?: string
  total_count: number
  matched_count: number
  progress_current?: number
  progress_total?: number
  duration_ms: number
  error: string
  created_at: string
  started_at?: string
  finished_at: string
  results?: ScreenerResultItem[]
}

export interface ScreenerProviderCatalogItem {
  name: string
  label: string
  type: string
  status: string
  available: boolean
  configured: boolean
  description: string
}

export interface ScreenerFunctionCatalog {
  fields: Array<{ name: string; description: string }>
  functions: Array<{ name: string; description: string }>
  examples: Array<{ name: string; formula: string }>
}

export interface ScreenerFormulaPayload {
  name: string
  description?: string
  formula: string
  universe_config?: Record<string, any>
  enabled?: boolean
}

export interface ScreenerRunPayload {
  formula_id?: number | null
  formula?: string
  universe_config?: Record<string, any>
}

export const screenerApi = {
  providerCatalog: () =>
    fetchAPI<{ items: ScreenerProviderCatalogItem[] }>('/providers/catalog?type=screener'),
  functions: () => fetchAPI<ScreenerFunctionCatalog>('/screener/functions'),
  listFormulas: () => fetchAPI<{ items: ScreenerFormulaItem[] }>('/screener/formulas'),
  createFormula: (payload: ScreenerFormulaPayload) =>
    fetchAPI<ScreenerFormulaItem>('/screener/formulas', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateFormula: (id: number, payload: ScreenerFormulaPayload) =>
    fetchAPI<ScreenerFormulaItem>(`/screener/formulas/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteFormula: (id: number) =>
    fetchAPI<{ ok: boolean }>(`/screener/formulas/${id}`, { method: 'DELETE' }),
  validateFormula: (formula: string) =>
    fetchAPI<{ valid: boolean; message: string }>('/screener/formulas/validate', {
      method: 'POST',
      body: JSON.stringify({ formula }),
    }),
  createRun: (payload: ScreenerRunPayload) =>
    fetchAPI<ScreenerRunItem>('/screener/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }),
  getRun: (id: number) =>
    fetchAPI<ScreenerRunItem>(`/screener/runs/${id}`, { timeoutMs: 30000 }),
  listRuns: (limit = 20) =>
    fetchAPI<{ items: ScreenerRunItem[] }>(`/screener/runs?limit=${encodeURIComponent(String(limit))}`),
}
