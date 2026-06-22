import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

interface TMonitorState {
  id: number
  position_id: number
  stock_symbol: string
  stock_name: string
  trade_date: string
  state: string
  score: number
  recommended_quantity: number
  current_price: number | null
  vwap: number | null
  support_price: number | null
  stop_loss_price: number | null
  target_price: number | null
  context?: { reason?: string; data_quality?: string }
}

const stateLabels: Record<string, string> = {
  idle: '观察',
  buy_t_notified: '待确认低吸',
  waiting_exit: '等待反弹',
  sell_t_notified: '待确认卖出',
  completed: '今日完成',
  invalidated: '已失效',
}

const price = (value: number | null) => value == null ? '--' : value.toFixed(3)

export default function TMonitorPanel() {
  const [rows, setRows] = useState<TMonitorState[]>([])
  const [loading, setLoading] = useState(false)
  const { toast } = useToast()

  const load = useCallback(async () => {
    try {
      setRows(await fetchAPI<TMonitorState[]>('/t-monitor/states'))
    } catch {
      setRows([])
    }
  }, [])

  useEffect(() => { load() }, [load])

  const scan = async () => {
    setLoading(true)
    try {
      const result = await fetchAPI<{ scanned: number; triggered: number }>('/t-monitor/scan', { method: 'POST' })
      toast(`扫描 ${result.scanned} 只持仓，触发 ${result.triggered} 条信号`, 'success')
      await load()
    } catch (error) {
      toast(error instanceof Error ? error.message : '扫描失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const confirm = async (row: TMonitorState, action: 'buy' | 'sell') => {
    await fetchAPI(`/t-monitor/states/${row.id}/confirm-${action}`, { method: 'POST' })
    toast(action === 'buy' ? '已确认买入 T 仓，开始监控卖点' : '已确认卖出，今日做 T 完成', 'success')
    await load()
  }

  return (
    <div className="card p-4 mb-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <div className="text-[13px] font-semibold text-foreground">底仓 VWAP 回归做T</div>
          <div className="text-[11px] text-muted-foreground mt-0.5">仅监控 A 股持仓并提醒，不自动交易；低吸后须确认才会监控卖点</div>
        </div>
        <Button size="sm" variant="outline" onClick={scan} disabled={loading}>
          <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />立即扫描
        </Button>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border/60 py-5 text-center text-[12px] text-muted-foreground">尚无今日扫描结果</div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5">
          {rows.map(row => (
            <div key={row.id} className="rounded-lg border border-border/50 bg-background/30 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-[13px] font-medium">{row.stock_name} <span className="font-mono text-muted-foreground">{row.stock_symbol}</span></div>
                <div className="flex items-center gap-1.5"><Badge variant="secondary">T Score {Math.round(row.score)}</Badge><Badge>{stateLabels[row.state] || row.state}</Badge></div>
              </div>
              <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mt-3 text-[11px]">
                <div><span className="text-muted-foreground">现价</span><div className="font-mono">{price(row.current_price)}</div></div>
                <div><span className="text-muted-foreground">VWAP</span><div className="font-mono">{price(row.vwap)}</div></div>
                <div><span className="text-muted-foreground">支撑</span><div className="font-mono">{price(row.support_price)}</div></div>
                <div><span className="text-muted-foreground">止损</span><div className="font-mono text-emerald-600">{price(row.stop_loss_price)}</div></div>
                <div><span className="text-muted-foreground">目标</span><div className="font-mono text-rose-600">{price(row.target_price)}</div></div>
                <div><span className="text-muted-foreground">建议</span><div className="font-mono">{row.recommended_quantity || '--'} 股</div></div>
              </div>
              {row.context?.reason && <div className="text-[11px] text-muted-foreground mt-2 line-clamp-2">{row.context.reason}</div>}
              {row.state === 'buy_t_notified' && <Button size="sm" className="mt-2" onClick={() => confirm(row, 'buy')}>确认已低吸</Button>}
              {row.state === 'sell_t_notified' && <Button size="sm" className="mt-2" onClick={() => confirm(row, 'sell')}>确认已卖出</Button>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
