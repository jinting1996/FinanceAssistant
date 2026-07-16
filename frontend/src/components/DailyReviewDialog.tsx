import { useCallback, useEffect, useRef, useState } from 'react'
import { ClipboardCopy, Plus, RefreshCw, Trash2 } from 'lucide-react'
import {
  createTradeRecord,
  deleteTradeRecord,
  fetchDailyReview,
  listTradeRecords,
  type TradeRecordItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@panwatch/base-ui/components/ui/select'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@panwatch/base-ui/components/ui/tabs'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

interface AccountOption {
  id: number
  name: string
}

interface DailyReviewDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  accounts: AccountOption[]
}

const emptyTradeForm = {
  account_id: '',
  symbol: '',
  name: '',
  market: 'CN',
  direction: 'buy' as 'buy' | 'sell',
  price: '',
  quantity: '',
  note: '',
}

function todayStr(): string {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

/** 复制文本到剪贴板：HTTP 环境无 navigator.clipboard，回退 execCommand */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走回退方案
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}

export default function DailyReviewDialog({ open, onOpenChange, accounts }: DailyReviewDialogProps) {
  const { toast } = useToast()
  const [activeTab, setActiveTab] = useState('review')

  // ---- 复盘文本 ----
  const [markdown, setMarkdown] = useState('')
  const [reviewLoading, setReviewLoading] = useState(false)
  const [hideAmounts, setHideAmounts] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const loadReview = useCallback(async (hide: boolean) => {
    setReviewLoading(true)
    try {
      const result = await fetchDailyReview({ hide_amounts: hide })
      setMarkdown(result.markdown)
    } catch (e) {
      toast(e instanceof Error ? e.message : '生成复盘文本失败', 'error')
    } finally {
      setReviewLoading(false)
    }
  }, [toast])

  // ---- 今日成交 ----
  const [trades, setTrades] = useState<TradeRecordItem[]>([])
  const [tradesLoading, setTradesLoading] = useState(false)
  const [tradeForm, setTradeForm] = useState({ ...emptyTradeForm })
  const [saving, setSaving] = useState(false)

  const loadTrades = useCallback(async () => {
    setTradesLoading(true)
    try {
      setTrades(await listTradeRecords({ date: todayStr() }))
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载成交记录失败', 'error')
    } finally {
      setTradesLoading(false)
    }
  }, [toast])

  useEffect(() => {
    if (!open) return
    loadReview(hideAmounts)
    loadTrades()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleToggleHideAmounts = (checked: boolean) => {
    setHideAmounts(checked)
    loadReview(checked)
  }

  const handleCopy = async () => {
    if (!markdown) return
    const ok = await copyText(markdown)
    toast(ok ? '复盘文本已复制到剪贴板' : '复制失败，请手动全选复制', ok ? 'success' : 'error')
    if (!ok && textareaRef.current) {
      textareaRef.current.select()
    }
  }

  const handleSaveTrade = async () => {
    const price = parseFloat(tradeForm.price)
    const quantity = parseInt(tradeForm.quantity)
    if (!tradeForm.symbol.trim()) {
      toast('请填写股票代码', 'error')
      return
    }
    if (!price || price <= 0 || !quantity || quantity <= 0) {
      toast('请填写有效的成交价与数量', 'error')
      return
    }
    setSaving(true)
    try {
      await createTradeRecord({
        account_id: tradeForm.account_id ? parseInt(tradeForm.account_id) : undefined,
        symbol: tradeForm.symbol.trim(),
        market: tradeForm.market,
        name: tradeForm.name.trim(),
        direction: tradeForm.direction,
        price,
        quantity,
        note: tradeForm.note.trim(),
      })
      toast('成交已录入', 'success')
      setTradeForm(f => ({ ...emptyTradeForm, account_id: f.account_id, market: f.market }))
      loadTrades()
    } catch (e) {
      toast(e instanceof Error ? e.message : '录入成交失败', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleDeleteTrade = async (id: number) => {
    if (!confirm('确定删除这笔成交记录？')) return
    try {
      await deleteTradeRecord(id)
      toast('成交记录已删除', 'success')
      loadTrades()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>每日复盘</DialogTitle>
          <DialogDescription>生成可复制的复盘数据文本，或录入今日成交</DialogDescription>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList>
            <TabsTrigger value="review">复盘文本</TabsTrigger>
            <TabsTrigger value="trades">今日成交 {trades.length > 0 ? `(${trades.length})` : ''}</TabsTrigger>
          </TabsList>

          <TabsContent value="review" className="space-y-3">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                <Switch checked={hideAmounts} onCheckedChange={handleToggleHideAmounts} className="scale-90" />
                <span className="text-[12px] text-muted-foreground">隐藏绝对金额（仅比例）</span>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="secondary" size="sm" onClick={() => loadReview(hideAmounts)} disabled={reviewLoading}>
                  <RefreshCw className={`w-3.5 h-3.5 ${reviewLoading ? 'animate-spin' : ''}`} /> 重新生成
                </Button>
                <Button size="sm" onClick={handleCopy} disabled={!markdown || reviewLoading}>
                  <ClipboardCopy className="w-3.5 h-3.5" /> 复制复盘文本
                </Button>
              </div>
            </div>
            <textarea
              ref={textareaRef}
              readOnly
              value={reviewLoading ? '生成中…' : markdown}
              className="w-full h-[45vh] rounded-md border border-border bg-accent/20 p-3 font-mono text-[12px] leading-relaxed text-foreground resize-none focus:outline-none"
            />
          </TabsContent>

          <TabsContent value="trades" className="space-y-4">
            {/* 录入表单 */}
            <div className="rounded-lg border border-border p-3 space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                <div className="space-y-1">
                  <Label className="text-[11px]">账户</Label>
                  <Select value={tradeForm.account_id} onValueChange={v => setTradeForm(f => ({ ...f, account_id: v }))}>
                    <SelectTrigger className="h-8 text-[12px]"><SelectValue placeholder="选择账户" /></SelectTrigger>
                    <SelectContent>
                      {accounts.map(a => (
                        <SelectItem key={a.id} value={String(a.id)}>{a.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">市场</Label>
                  <Select value={tradeForm.market} onValueChange={v => setTradeForm(f => ({ ...f, market: v }))}>
                    <SelectTrigger className="h-8 text-[12px]"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="CN">A股</SelectItem>
                      <SelectItem value="HK">港股</SelectItem>
                      <SelectItem value="US">美股</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">代码</Label>
                  <Input className="h-8 text-[12px]" placeholder="600519" value={tradeForm.symbol}
                    onChange={e => setTradeForm(f => ({ ...f, symbol: e.target.value }))} />
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">名称（可选）</Label>
                  <Input className="h-8 text-[12px]" placeholder="贵州茅台" value={tradeForm.name}
                    onChange={e => setTradeForm(f => ({ ...f, name: e.target.value }))} />
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">方向</Label>
                  <Select value={tradeForm.direction} onValueChange={v => setTradeForm(f => ({ ...f, direction: v as 'buy' | 'sell' }))}>
                    <SelectTrigger className="h-8 text-[12px]"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="buy">买入</SelectItem>
                      <SelectItem value="sell">卖出</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">成交价</Label>
                  <Input className="h-8 text-[12px]" type="number" step="0.001" placeholder="0.00" value={tradeForm.price}
                    onChange={e => setTradeForm(f => ({ ...f, price: e.target.value }))} />
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">数量</Label>
                  <Input className="h-8 text-[12px]" type="number" step="100" placeholder="100" value={tradeForm.quantity}
                    onChange={e => setTradeForm(f => ({ ...f, quantity: e.target.value }))} />
                </div>
                <div className="space-y-1">
                  <Label className="text-[11px]">备注（可选）</Label>
                  <Input className="h-8 text-[12px]" placeholder="按计划止盈…" value={tradeForm.note}
                    onChange={e => setTradeForm(f => ({ ...f, note: e.target.value }))} />
                </div>
              </div>
              <div className="flex justify-end">
                <Button size="sm" onClick={handleSaveTrade} disabled={saving}>
                  <Plus className="w-3.5 h-3.5" /> {saving ? '保存中…' : '录入成交'}
                </Button>
              </div>
            </div>

            {/* 今日成交列表 */}
            {tradesLoading ? (
              <p className="text-[12px] text-muted-foreground text-center py-4">加载中…</p>
            ) : trades.length === 0 ? (
              <p className="text-[12px] text-muted-foreground text-center py-4">今日暂无成交记录</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground">
                      <th className="text-left px-2 py-1.5 font-semibold">时间</th>
                      <th className="text-left px-2 py-1.5 font-semibold">代码</th>
                      <th className="text-left px-2 py-1.5 font-semibold">名称</th>
                      <th className="text-left px-2 py-1.5 font-semibold">方向</th>
                      <th className="text-right px-2 py-1.5 font-semibold">价格</th>
                      <th className="text-right px-2 py-1.5 font-semibold">数量</th>
                      <th className="text-right px-2 py-1.5 font-semibold">金额</th>
                      <th className="px-2 py-1.5"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map(t => (
                      <tr key={t.id} className="border-b border-border/50">
                        <td className="px-2 py-1.5">{t.traded_at ? t.traded_at.slice(11, 16) : '—'}</td>
                        <td className="px-2 py-1.5 font-mono">{t.symbol}</td>
                        <td className="px-2 py-1.5">{t.name || '—'}</td>
                        <td className={`px-2 py-1.5 ${t.direction === 'buy' ? 'text-red-500' : 'text-emerald-600'}`}>
                          {t.direction === 'buy' ? '买入' : '卖出'}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono">{t.price.toFixed(3)}</td>
                        <td className="px-2 py-1.5 text-right font-mono">{t.quantity}</td>
                        <td className="px-2 py-1.5 text-right font-mono">{t.amount.toFixed(2)}</td>
                        <td className="px-2 py-1.5 text-right">
                          <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => handleDeleteTrade(t.id)}>
                            <Trash2 className="w-3.5 h-3.5 text-muted-foreground" />
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}
