import { useCallback, useEffect, useState } from 'react'
import { Activity, Droplets } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

interface SectorFlow {
  name: string
  main_net_inflow_yi: number
  main_net_ratio_pct: number | null
  change_pct: number | null
}

interface MarketMood {
  sector_flows: SectorFlow[]
  market_flow: {
    date: string
    main_net_inflow_yi: number | null
    sh_change_pct: number | null
    sz_change_pct: number | null
  } | null
  activity: {
    up_count: number | null
    down_count: number | null
    limit_up_count: number | null
    limit_down_count: number | null
    activity_pct: number | null
  } | null
  sentiment: {
    score: number
    label: string
    confidence: number
  } | null
  errors?: string[]
  updated_at: string
}

const REFRESH_MS = 5 * 60 * 1000

const sentimentColor = (score: number) =>
  score >= 60 ? 'text-rose-500' : score < 40 ? 'text-emerald-500' : 'text-amber-500'

const sentimentBar = (score: number) =>
  score >= 60 ? 'bg-rose-500' : score < 40 ? 'bg-emerald-500' : 'bg-amber-500'

const flowText = (v: number) => (v >= 0 ? `+${v.toFixed(1)}亿` : `${v.toFixed(1)}亿`)

export default function MarketMoodCards() {
  const [mood, setMood] = useState<MarketMood | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    try {
      setMood(await fetchAPI<MarketMood>('/market-mood'))
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  const sentiment = mood?.sentiment
  const activity = mood?.activity
  const marketFlow = mood?.market_flow
  const inflows = (mood?.sector_flows || []).filter(s => s.main_net_inflow_yi > 0).slice(0, 3)
  const outflows = (mood?.sector_flows || [])
    .filter(s => s.main_net_inflow_yi < 0)
    .sort((a, b) => a.main_net_inflow_yi - b.main_net_inflow_yi)
    .slice(0, 3)
  const maxAbs = Math.max(1, ...[...inflows, ...outflows].map(s => Math.abs(s.main_net_inflow_yi)))
  const sectorError = (mood?.errors || []).find(e => e.includes('板块资金流'))

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
      {/* 大盘情绪 */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-1.5 text-muted-foreground">
            <Activity className="w-4 h-4" />
            <span className="text-[12px]">大盘情绪</span>
          </div>
          {mood?.updated_at && (
            <span className="text-[10px] text-muted-foreground/60 font-mono">{mood.updated_at.slice(11, 16)}</span>
          )}
        </div>
        {sentiment ? (
          <>
            <div className={`text-[26px] leading-none font-bold font-mono ${sentimentColor(sentiment.score)}`}>
              {sentiment.score.toFixed(0)}
              <span className="text-[14px] ml-2">{sentiment.label}</span>
            </div>
            <div className="mt-2 h-1.5 rounded-full bg-accent/50 overflow-hidden">
              <div
                className={`h-full rounded-full ${sentimentBar(sentiment.score)}`}
                style={{ width: `${Math.min(100, Math.max(0, sentiment.score))}%` }}
              />
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
              {activity && activity.up_count != null && (
                <span>
                  涨<b className="text-rose-500 mx-0.5">{activity.up_count}</b>/
                  跌<b className="text-emerald-500 mx-0.5">{activity.down_count}</b>
                </span>
              )}
              {activity && activity.limit_up_count != null && (
                <span>
                  涨停<b className="text-rose-500 mx-0.5">{activity.limit_up_count}</b>
                  跌停<b className="text-emerald-500 mx-0.5">{activity.limit_down_count}</b>
                </span>
              )}
              {marketFlow?.main_net_inflow_yi != null && (
                <span>
                  主力
                  <b className={`mx-0.5 ${marketFlow.main_net_inflow_yi >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                    {flowText(marketFlow.main_net_inflow_yi)}
                  </b>
                </span>
              )}
            </div>
          </>
        ) : (
          <div className="text-[12px] text-muted-foreground py-3">{error || mood ? '情绪数据获取失败' : '加载中…'}</div>
        )}
      </div>

      {/* 板块资金流 */}
      <div className="card p-4">
        <div className="flex items-center gap-1.5 text-muted-foreground mb-1.5">
          <Droplets className="w-4 h-4" />
          <span className="text-[12px]">板块主力资金(今日)</span>
        </div>
        {inflows.length || outflows.length ? (
          <div className="grid grid-cols-2 gap-x-3">
            {[inflows, outflows].map((list, i) => (
              <div key={i} className="space-y-1">
                {list.map(s => {
                  const inflow = s.main_net_inflow_yi >= 0
                  return (
                    <div key={s.name} className="flex items-center gap-1.5" title={`${s.name} 主力净${inflow ? '流入' : '流出'} ${Math.abs(s.main_net_inflow_yi).toFixed(2)} 亿${s.change_pct != null ? `,涨跌幅 ${s.change_pct}%` : ''}`}>
                      <span className="w-14 shrink-0 truncate text-[11px] text-foreground">{s.name}</span>
                      <div className="flex-1 h-1.5 rounded-full bg-accent/40 overflow-hidden">
                        <div
                          className={`h-full rounded-full ${inflow ? 'bg-rose-500/80' : 'bg-emerald-500/80'}`}
                          style={{ width: `${Math.max(6, (Math.abs(s.main_net_inflow_yi) / maxAbs) * 100)}%` }}
                        />
                      </div>
                      <span className={`w-14 shrink-0 text-right text-[10px] font-mono ${inflow ? 'text-rose-500' : 'text-emerald-500'}`}>
                        {flowText(s.main_net_inflow_yi)}
                      </span>
                    </div>
                  )
                })}
                {!list.length && <div className="text-[10px] text-muted-foreground py-1">{i === 0 ? '无净流入板块' : '无净流出板块'}</div>}
              </div>
            ))}
          </div>
        ) : mood || error ? (
          <div className="py-2">
            <div className="text-[12px] text-muted-foreground">板块资金流获取失败</div>
            {sectorError && (
              <div className="mt-1 text-[10px] text-amber-600 dark:text-amber-400 break-all line-clamp-3" title={sectorError}>
                {sectorError}
              </div>
            )}
          </div>
        ) : (
          <div className="text-[12px] text-muted-foreground py-3">加载中…</div>
        )}
      </div>
    </div>
  )
}
