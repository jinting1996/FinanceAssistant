import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Settings2 } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

type LegAction = 'long_open' | 'short_open' | 'long_close' | 'short_close'

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
  context?: {
    reason?: string
    data_quality?: string
    direction?: string
    skip_reason?: string
    score_detail?: Record<string, boolean>
    score_side?: string
  }
}

// 做T评分的各加分项:key 对应后端 score_detail,label 按多/空方向区分文案
const SCORE_CHECKS: { key: string; weight: number; label: (short: boolean) => string; hint: (short: boolean) => string }[] = [
  { key: 'trend', weight: 20, label: () => '趋势', hint: s => s ? '日线未单边强势上涨,适合高抛' : '日线趋势未破且 MA20 未明显向下' },
  { key: 'support', weight: 20, label: s => s ? '近压力' : '近支撑', hint: s => s ? '当前价接近压力位' : '当前价接近支撑位' },
  { key: 'vwap', weight: 15, label: s => s ? '高于VWAP' : '低于VWAP', hint: s => s ? '当前价高于 VWAP 足够幅度' : '当前价低于 VWAP 足够幅度' },
  { key: 'reversal', weight: 20, label: s => s ? '滞涨反转' : '止跌反转', hint: s => s ? '最近三根分钟K高点走低并滞涨' : '最近三根分钟K低点抬高并止跌' },
  { key: 'coverage', weight: 10, label: () => '数据充足', hint: () => '分钟数据覆盖满足盘中判断' },
  { key: 'reward', weight: 15, label: () => '盈亏比', hint: () => '预期盈亏比 ≥ 1' },
]

const stateLabels: Record<string, string> = {
  idle: '观察',
  buy_t_notified: '待确认低吸',
  waiting_exit: '等待反弹',
  sell_t_notified: '待确认卖出',
  sell_open_notified: '待确认高抛',
  waiting_buyback: '等待回落',
  buy_back_notified: '待确认买回',
  completed: '今日完成',
  invalidated: '已失效',
}

// 倒T(先卖后买)涉及的状态
const SHORT_STATES = ['sell_open_notified', 'waiting_buyback', 'buy_back_notified']
const isShortRow = (row: TMonitorState) =>
  row.context?.direction === 'short' || SHORT_STATES.includes(row.state)

const price = (value: number | null) => value == null ? '--' : value.toFixed(3)

const LEG_TITLES: Record<LegAction, string> = {
  long_open: '确认低吸买入',
  short_open: '确认高抛卖出',
  long_close: '确认止盈卖出',
  short_close: '确认买回平仓',
}

export default function TMonitorPanel() {
  const [rows, setRows] = useState<TMonitorState[]>([])
  const [loading, setLoading] = useState(false)
  const [leg, setLeg] = useState<{ row: TMonitorState; action: LegAction } | null>(null)
  const [legPrice, setLegPrice] = useState('')
  const [legQty, setLegQty] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [params, setParams] = useState<Record<string, any>>({})
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

  const openLeg = (row: TMonitorState, action: LegAction) => {
    setLeg({ row, action })
    setLegPrice(row.current_price != null ? String(row.current_price) : '')
    setLegQty(row.recommended_quantity ? String(row.recommended_quantity) : '')
  }

  const submitLeg = async () => {
    if (!leg) return
    const price = parseFloat(legPrice)
    const quantity = parseInt(legQty)
    if (!(price > 0) || !(quantity > 0)) {
      toast('请输入有效的成交价与数量', 'error')
      return
    }
    try {
      const r = await fetchAPI<{ realized?: number; new_cost_price?: number }>(
        `/t-monitor/states/${leg.row.id}/execute`,
        { method: 'POST', body: JSON.stringify({ action: leg.action, price, quantity }) },
      )
      if (leg.action.endsWith('_close')) {
        const profit = r.realized ?? 0
        toast(`本轮做T ${profit >= 0 ? '盈利' : '亏损'} ${profit.toFixed(2)} 元${r.new_cost_price != null ? `，成本摊低至 ${r.new_cost_price}` : ''}`, 'success')
      } else {
        toast('已记录成交，开始监控对侧点位', 'success')
      }
      setLeg(null)
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const reset = async (row: TMonitorState) => {
    try {
      await fetchAPI(`/t-monitor/states/${row.id}/manual?action=reset`, { method: 'POST' })
      toast('已重置', 'success')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const openSettings = async () => {
    try {
      const p = await fetchAPI<Record<string, unknown>>('/t-monitor/params')
      setParams(p || {})
      setSettingsOpen(true)
    } catch (e) {
      toast(e instanceof Error ? e.message : '读取参数失败', 'error')
    }
  }

  const setParam = (key: string, value: unknown) => setParams(prev => ({ ...prev, [key]: value }))

  const saveSettings = async () => {
    try {
      const body = {
        direction: params.direction,
        exit_mode: params.exit_mode,
        min_score: Number(params.min_score),
        position_ratio: Number(params.position_ratio),
        max_cycles_per_day: Number(params.max_cycles_per_day),
        cycle_cooldown_minutes: Number(params.cycle_cooldown_minutes),
        trail_pct: Number(params.trail_pct),
        profit_atr_mult: Number(params.profit_atr_mult),
        stop_atr_mult: Number(params.stop_atr_mult),
      }
      await fetchAPI('/t-monitor/params', { method: 'PUT', body: JSON.stringify(body) })
      toast('参数已保存', 'success')
      setSettingsOpen(false)
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    }
  }

  return (
    <div className="card p-4 mb-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <div className="text-[13px] font-semibold text-foreground">底仓 VWAP 回归做T</div>
          <div className="text-[11px] text-muted-foreground mt-0.5">仅监控 A 股持仓并提醒，不自动交易；低吸后须确认才会监控卖点</div>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={openSettings}>
            <Settings2 className="w-3.5 h-3.5 mr-1.5" />参数
          </Button>
          <Button size="sm" variant="outline" onClick={scan} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />立即扫描
          </Button>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border/60 py-5 text-center text-[12px] text-muted-foreground">尚无今日扫描结果</div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5">
          {rows.map(row => {
            const short = isShortRow(row)
            return (
            <div key={row.id} className="rounded-lg border border-border/50 bg-background/30 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-[13px] font-medium">{row.stock_name} <span className="font-mono text-muted-foreground">{row.stock_symbol}</span></div>
                <div className="flex items-center gap-1.5">
                  {row.state !== 'idle' && (
                    <Badge variant="outline" className={short ? 'text-emerald-600 border-emerald-500/40' : 'text-rose-600 border-rose-500/40'}>
                      {short ? '倒T' : '正T'}
                    </Badge>
                  )}
                  {['idle', 'waiting_exit', 'waiting_buyback'].includes(row.state) && (
                    <Badge variant="secondary" title={row.state === 'waiting_buyback' ? '买回质量分' : row.state === 'waiting_exit' ? '卖出质量分' : '入场质量分'}>
                      {row.state === 'waiting_buyback' ? '买回分' : row.state === 'waiting_exit' ? '卖出分' : 'T Score'} {Math.round(row.score)}
                    </Badge>
                  )}
                  <Badge>{stateLabels[row.state] || row.state}</Badge>
                </div>
              </div>
              <div className="grid grid-cols-3 sm:grid-cols-6 gap-1.5 mt-3">
                {[
                  { label: '现价', value: price(row.current_price), cls: 'text-foreground' },
                  { label: 'VWAP', value: price(row.vwap), cls: 'text-foreground' },
                  { label: short ? '压力' : '支撑', value: price(row.support_price), cls: 'text-foreground' },
                  { label: '止损', value: price(row.stop_loss_price), cls: short ? 'text-rose-600' : 'text-emerald-600' },
                  { label: '目标', value: price(row.target_price), cls: short ? 'text-emerald-600' : 'text-rose-600' },
                ].map(f => (
                  <div key={f.label} className="rounded-md bg-background/40 px-1.5 py-1">
                    <div className="text-[10px] leading-tight text-muted-foreground">{f.label}</div>
                    <div className={`text-[12px] font-mono font-medium ${f.cls}`}>{f.value}</div>
                  </div>
                ))}
                <div className="rounded-md border border-primary/20 bg-primary/10 px-1.5 py-1">
                  <div className="text-[10px] leading-tight text-primary/70">建议</div>
                  <div className="text-[12px] font-mono font-semibold text-primary">
                    {row.recommended_quantity || '--'}<span className="ml-0.5 text-[10px] font-normal">股</span>
                  </div>
                </div>
              </div>
              {row.context?.score_detail && (
                <div className="mt-2 flex flex-wrap items-center gap-1">
                  {SCORE_CHECKS.map(check => {
                    const scoreShort = row.context?.score_side === 'short'
                    const hit = !!row.context?.score_detail?.[check.key]
                    return (
                      <span
                        key={check.key}
                        title={`${check.hint(scoreShort)}(+${check.weight}分)${hit ? '' : ',当前未得分'}`}
                        className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] leading-tight border ${
                          hit
                            ? scoreShort
                              ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                              : 'border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                            : 'border-border/50 bg-muted/40 text-muted-foreground/60'
                        }`}
                      >
                        {check.label(scoreShort)} +{check.weight}
                      </span>
                    )
                  })}
                </div>
              )}
              {row.context?.reason && <div className="text-[11px] text-muted-foreground mt-2 line-clamp-2">{row.context.reason}</div>}
              {row.context?.skip_reason && (
                <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-700 dark:text-amber-400">
                  ⚠ 已达标但未触发:{row.context.skip_reason}
                </div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {/* 自动流程的确认按钮(点开后输入实际成交价+数量) */}
                {row.state === 'buy_t_notified' && <Button size="sm" onClick={() => openLeg(row, 'long_open')}>确认已低吸</Button>}
                {row.state === 'sell_t_notified' && <Button size="sm" onClick={() => openLeg(row, 'long_close')}>确认已卖出</Button>}
                {row.state === 'sell_open_notified' && <Button size="sm" onClick={() => openLeg(row, 'short_open')}>确认已高抛</Button>}
                {row.state === 'buy_back_notified' && <Button size="sm" onClick={() => openLeg(row, 'short_close')}>确认已买回</Button>}

                {/* 等待态:手动标记完成(输入平仓价) */}
                {row.state === 'waiting_exit' && <Button size="sm" variant="outline" onClick={() => openLeg(row, 'long_close')}>我已卖出完成</Button>}
                {row.state === 'waiting_buyback' && <Button size="sm" variant="outline" onClick={() => openLeg(row, 'short_close')}>我已买回完成</Button>}

                {/* 非活跃态:手动入场(策略没提示时,记录你的实际操作) */}
                {['idle', 'completed', 'invalidated'].includes(row.state) && (
                  <>
                    <Button size="sm" variant="outline" onClick={() => openLeg(row, 'long_open')}>我已低吸 → 盯卖点</Button>
                    <Button size="sm" variant="outline" onClick={() => openLeg(row, 'short_open')}>我已高抛 → 盯买点</Button>
                  </>
                )}

                {/* 重置:任何非 idle 态可清回观察 */}
                {row.state !== 'idle' && <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={() => reset(row)}>重置</Button>}
              </div>
            </div>
            )
          })}
        </div>
      )}

      <Dialog open={!!leg} onOpenChange={(o) => { if (!o) setLeg(null) }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-[15px]">
              {leg ? LEG_TITLES[leg.action] : ''}
              {leg && <span className="ml-2 text-[12px] font-normal text-muted-foreground">{leg.row.stock_name} {leg.row.stock_symbol}</span>}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>成交价</Label>
              <Input value={legPrice} onChange={e => setLegPrice(e.target.value)} inputMode="decimal" className="font-mono mt-1" placeholder="实际成交价" />
            </div>
            <div>
              <Label>数量(股)</Label>
              <Input value={legQty} onChange={e => setLegQty(e.target.value)} inputMode="numeric" className="font-mono mt-1" placeholder="实际成交数量" />
            </div>
            {leg?.action.endsWith('_close') && (
              <div className="text-[11px] text-muted-foreground">平仓后将按 数量×(卖出价−买入价) 计算做T盈亏,并摊低该持仓成本。</div>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="outline" size="sm" onClick={() => setLeg(null)}>取消</Button>
              <Button size="sm" onClick={submitLeg}>确认</Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-w-md max-h-[82vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-[15px]">做T 策略参数</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-[12px]">
            <div>
              <Label>方向</Label>
              <select
                value={params.direction || 'both'}
                onChange={e => setParam('direction', e.target.value)}
                className="mt-1 w-full h-8 rounded-md border border-border bg-background px-2 text-[12px]"
              >
                <option value="both">双向(正T + 倒T)</option>
                <option value="long">仅正T(先低吸后卖)</option>
                <option value="short">仅倒T(先高抛后买回)</option>
              </select>
            </div>
            <div>
              <Label>离场方式</Label>
              <select
                value={params.exit_mode || 'price'}
                onChange={e => setParam('exit_mode', e.target.value)}
                className="mt-1 w-full h-8 rounded-md border border-border bg-background px-2 text-[12px]"
              >
                <option value="price">固定价(到目标就走,可预期)</option>
                <option value="trail">跟踪止盈(跟着趋势多吃)</option>
                <option value="price_or_score">价格或评分(可提前出)</option>
              </select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label>触发分</Label>
                <Input value={params.min_score ?? ''} onChange={e => setParam('min_score', e.target.value)} inputMode="numeric" className="font-mono mt-1 h-8" />
                <p className="text-[11px] text-muted-foreground mt-0.5">≥此分才出信号(满分100,越高越严)</p>
              </div>
              <div>
                <Label>仓位比例</Label>
                <Input value={params.position_ratio ?? ''} onChange={e => setParam('position_ratio', e.target.value)} inputMode="decimal" className="font-mono mt-1 h-8" />
                <p className="text-[11px] text-muted-foreground mt-0.5">每轮动用可卖底仓比例(0.2=20%)</p>
              </div>
              <div>
                <Label>当日轮数</Label>
                <Input value={params.max_cycles_per_day ?? ''} onChange={e => setParam('max_cycles_per_day', e.target.value)} inputMode="numeric" className="font-mono mt-1 h-8" />
                <p className="text-[11px] text-muted-foreground mt-0.5">每票当日最多做几轮(0=不限)</p>
              </div>
              <div>
                <Label>轮间冷却(分)</Label>
                <Input value={params.cycle_cooldown_minutes ?? ''} onChange={e => setParam('cycle_cooldown_minutes', e.target.value)} inputMode="numeric" className="font-mono mt-1 h-8" />
                <p className="text-[11px] text-muted-foreground mt-0.5">完成一轮后多久才再开</p>
              </div>
              <div>
                <Label>跟踪回撤</Label>
                <Input value={params.trail_pct ?? ''} onChange={e => setParam('trail_pct', e.target.value)} inputMode="decimal" className="font-mono mt-1 h-8" />
                <p className="text-[11px] text-muted-foreground mt-0.5">trail模式:自极值回撤多少离场(0.003=0.3%)</p>
              </div>
            </div>

            <div className="rounded-md border border-primary/20 bg-primary/5 p-2.5">
              <Label>止盈 ATR 倍数(profit_atr_mult)</Label>
              <Input value={params.profit_atr_mult ?? ''} onChange={e => setParam('profit_atr_mult', e.target.value)} inputMode="decimal" className="font-mono mt-1 h-8" />
              <p className="text-[11px] text-muted-foreground mt-1 leading-4">
                控制<b>止盈目标随股票波动放大多少</b>。止盈 = max(地板0.8%, <b>该倍数 × 日波动ATR/股价</b>)。
                倍数越大 → 振幅大的票止盈挂得越远、一轮想吃的价差越大;0.5 ≈ 吃半个日波幅。振幅小的票仍走 0.8% 地板,无需手动按票调。
              </p>
            </div>
            <div className="rounded-md border border-rose-500/20 bg-rose-500/5 p-2.5">
              <Label>止损 ATR 倍数(stop_atr_mult)</Label>
              <Input value={params.stop_atr_mult ?? ''} onChange={e => setParam('stop_atr_mult', e.target.value)} inputMode="decimal" className="font-mono mt-1 h-8" />
              <p className="text-[11px] text-muted-foreground mt-1 leading-4">
                控制<b>止损上限随波动放宽多少</b>。止损上限 = max(地板1.5%, <b>该倍数 × ATR/股价</b>)。
                倍数越大 → 振幅大的票允许的止损更宽(信号不易被 1.5% 卡掉,但单次亏损上限更大);越小 → 止损更紧、过滤更多。
              </p>
            </div>

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="outline" size="sm" onClick={() => setSettingsOpen(false)}>取消</Button>
              <Button size="sm" onClick={saveSettings}>保存</Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
