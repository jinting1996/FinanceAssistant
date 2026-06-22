export type SuggestionAction =
  | 'buy'
  | 'add'
  | 'reduce'
  | 'sell'
  | 'hold'
  | 'watch'
  | 'alert'
  | 'avoid'

// 默认弱化为描边/淡底 + 彩色文字,hover 才填充高亮,避免列表里整列实心色块抢视线。
export const suggestionActionColors: Record<SuggestionAction, string> = {
  buy: 'bg-rose-500/10 text-rose-600 border border-rose-500/30 transition-colors hover:bg-rose-500 hover:text-white',
  add: 'bg-rose-400/10 text-rose-600 border border-rose-400/30 transition-colors hover:bg-rose-400 hover:text-white',
  reduce: 'bg-emerald-500/10 text-emerald-600 border border-emerald-500/30 transition-colors hover:bg-emerald-500 hover:text-white',
  sell: 'bg-emerald-600/10 text-emerald-700 border border-emerald-600/30 transition-colors hover:bg-emerald-600 hover:text-white',
  hold: 'bg-amber-500/10 text-amber-600 border border-amber-500/30 transition-colors hover:bg-amber-500 hover:text-white',
  watch: 'bg-slate-500/10 text-slate-600 border border-slate-500/30 transition-colors hover:bg-slate-500 hover:text-white',
  alert: 'bg-blue-500/10 text-blue-600 border border-blue-500/30 transition-colors hover:bg-blue-500 hover:text-white',
  avoid: 'bg-red-600/10 text-red-600 border border-red-600/30 transition-colors hover:bg-red-600 hover:text-white',
}

export const suggestionActionLabels: Record<SuggestionAction, string> = {
  buy: '买入',
  add: '加仓',
  reduce: '减仓',
  sell: '卖出',
  hold: '持有',
  watch: '观望',
  avoid: '回避',
  alert: '提醒',
}

export function normalizeSuggestionAction(action?: string, label?: string): SuggestionAction | null {
  const raw = (action || label || '').toLowerCase()
  if (!raw) return null
  if (raw === 'buy') return 'buy'
  if (raw === 'add' || raw === 'increase') return 'add'
  if (raw === 'reduce' || raw === 'decrease') return 'reduce'
  if (raw === 'sell') return 'sell'
  if (raw === 'hold') return 'hold'
  if (raw === 'watch' || raw === 'neutral') return 'watch'
  if (raw === 'avoid') return 'avoid'
  if (raw === 'alert') return 'alert'
  if (/买入|买|建仓/.test(raw)) return 'buy'
  if (/加仓|增持|补仓/.test(raw)) return 'add'
  if (/减仓|减持/.test(raw)) return 'reduce'
  if (/清仓|卖出|止损|卖/.test(raw)) return 'sell'
  if (/持有|持仓/.test(raw)) return 'hold'
  if (/观望|中性|等待/.test(raw)) return 'watch'
  if (/回避|规避|避免/.test(raw)) return 'avoid'
  return null
}

export function resolveSuggestionAction(action?: string, label?: string): SuggestionAction {
  return normalizeSuggestionAction(action, label) || 'watch'
}

export function resolveSuggestionLabel(action?: string, label?: string, fallback = '观望'): string {
  const normalized = normalizeSuggestionAction(action, label)
  if (normalized) return suggestionActionLabels[normalized] || fallback
  return String(label || '').trim() || fallback
}

export function resolveSuggestionColorClass(action?: string, label?: string): string {
  const normalized = resolveSuggestionAction(action, label)
  return suggestionActionColors[normalized] || suggestionActionColors.watch
}
