import { fetchAPI } from './client'

// ========== 成交流水 ==========

export interface TradeRecordItem {
  id: number
  account_id: number | null
  account_name: string | null
  symbol: string
  market: string
  name: string
  direction: 'buy' | 'sell'
  price: number
  quantity: number
  amount: number
  traded_at: string | null
  note: string
}

export interface TradeRecordCreatePayload {
  account_id?: number | null
  symbol: string
  market?: string
  name?: string
  direction: 'buy' | 'sell'
  price: number
  quantity: number
  amount?: number
  traded_at?: string
  note?: string
}

export function listTradeRecords(params?: { date?: string; account_id?: number }): Promise<TradeRecordItem[]> {
  const query = new URLSearchParams()
  if (params?.date) query.set('date', params.date)
  if (params?.account_id) query.set('account_id', String(params.account_id))
  const qs = query.toString()
  return fetchAPI<TradeRecordItem[]>(`/trades${qs ? `?${qs}` : ''}`)
}

export function createTradeRecord(payload: TradeRecordCreatePayload): Promise<TradeRecordItem> {
  return fetchAPI<TradeRecordItem>('/trades', { method: 'POST', body: JSON.stringify(payload) })
}

export function deleteTradeRecord(id: number): Promise<{ success: boolean }> {
  return fetchAPI<{ success: boolean }>(`/trades/${id}`, { method: 'DELETE' })
}

// ========== 每日复盘导出 ==========

export interface DailyReviewResult {
  date: string
  hide_amounts: boolean
  markdown: string
}

export function fetchDailyReview(params?: {
  date?: string
  account_id?: number
  hide_amounts?: boolean
}): Promise<DailyReviewResult> {
  const query = new URLSearchParams()
  if (params?.date) query.set('date', params.date)
  if (params?.account_id) query.set('account_id', String(params.account_id))
  if (params?.hide_amounts) query.set('hide_amounts', 'true')
  const qs = query.toString()
  return fetchAPI<DailyReviewResult>(`/review/daily${qs ? `?${qs}` : ''}`, { timeoutMs: 30000 })
}
