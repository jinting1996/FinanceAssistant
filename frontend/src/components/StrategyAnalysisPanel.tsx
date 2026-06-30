import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Bot,
  ChevronDown,
  Download,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Trash2,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  strategyAnalysisApi,
  type StrategyAnalysisResultItem,
  type StrategyPoolItem,
  type StrategyPromptItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'

const MARKET_LABEL: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股' }

export default function StrategyAnalysisPanel() {
  const [strategies, setStrategies] = useState<StrategyPromptItem[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [pool, setPool] = useState<StrategyPoolItem[]>([])
  const [results, setResults] = useState<Record<string, StrategyAnalysisResultItem>>({})

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [prompt, setPrompt] = useState('')
  const [editingPrompt, setEditingPrompt] = useState(false)

  const [newSymbol, setNewSymbol] = useState('')
  const [newMarket, setNewMarket] = useState('CN')

  const [saving, setSaving] = useState(false)
  const [importing, setImporting] = useState(false)
  const [adding, setAdding] = useState(false)
  const [analyzingAll, setAnalyzingAll] = useState(false)
  const [analyzingSymbol, setAnalyzingSymbol] = useState<string>('')
  const [message, setMessage] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const selected = strategies.find((s) => s.id === selectedId) || null
  const keyOf = (market: string, symbol: string) => `${market}:${symbol}`

  const applyStrategy = (row: StrategyPromptItem) => {
    setSelectedId(row.id)
    setName(row.name)
    setDescription(row.description || '')
    setPrompt(row.prompt || '')
    setEditingPrompt(false)
  }

  const loadAll = useCallback(async () => {
    const [stratRes, poolRes, resultRes] = await Promise.all([
      strategyAnalysisApi.listStrategies().catch(() => ({ items: [] })),
      strategyAnalysisApi.listPool().catch(() => ({ items: [] })),
      strategyAnalysisApi.listResults().catch(() => ({ items: [] })),
    ])
    const items = stratRes.items || []
    setStrategies(items)
    setPool(poolRes.items || [])
    const map: Record<string, StrategyAnalysisResultItem> = {}
    for (const r of resultRes.items || []) map[keyOf(r.market, r.symbol)] = r
    setResults(map)
    setSelectedId((prev) => {
      if (prev && items.some((x) => x.id === prev)) return prev
      const first = items[0]
      if (first) applyStrategy(first)
      return first ? first.id : null
    })
  }, [])

  useEffect(() => {
    loadAll().catch((e) => setMessage(e?.message || '加载策略分析失败'))
  }, [loadAll])

  const dirty = useMemo(() => {
    if (!selected) return name.trim() !== '' || prompt.trim() !== ''
    return (
      name !== selected.name ||
      description !== (selected.description || '') ||
      prompt !== (selected.prompt || '')
    )
  }, [selected, name, description, prompt])

  const saveStrategy = async () => {
    if (!name.trim() || !prompt.trim()) {
      setMessage('策略名称和提示词不能为空')
      return
    }
    setSaving(true)
    setMessage('')
    try {
      const payload = { name: name.trim(), description: description.trim(), prompt }
      const saved = selectedId
        ? await strategyAnalysisApi.updateStrategy(selectedId, payload)
        : await strategyAnalysisApi.createStrategy(payload)
      setMessage('策略已保存')
      setEditingPrompt(false)
      await loadAll()
      setSelectedId(saved.id)
    } catch (e: any) {
      setMessage(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const newStrategy = () => {
    setSelectedId(null)
    setName('新策略')
    setDescription('')
    setPrompt('')
    setEditingPrompt(true)
    setMessage('')
  }

  const removeStrategy = async () => {
    if (!selectedId || !selected || selected.is_default) return
    setSaving(true)
    try {
      await strategyAnalysisApi.deleteStrategy(selectedId)
      setMessage('策略已删除')
      setSelectedId(null)
      await loadAll()
    } catch (e: any) {
      setMessage(e?.message || '删除失败')
    } finally {
      setSaving(false)
    }
  }

  const importPositions = async () => {
    setImporting(true)
    setMessage('')
    try {
      const res = await strategyAnalysisApi.importPositions()
      setMessage(`已从持仓导入 ${res.created} 只（共扫描 ${res.scanned} 只）`)
      const poolRes = await strategyAnalysisApi.listPool()
      setPool(poolRes.items || [])
    } catch (e: any) {
      setMessage(e?.message || '导入持仓失败')
    } finally {
      setImporting(false)
    }
  }

  const addManual = async () => {
    const symbol = newSymbol.trim()
    if (!symbol) return
    setAdding(true)
    setMessage('')
    try {
      const res = await strategyAnalysisApi.addPoolItem({ symbol, market: newMarket })
      setMessage(res.created ? `已加入 ${symbol}` : `${symbol} 已在池中`)
      setNewSymbol('')
      const poolRes = await strategyAnalysisApi.listPool()
      setPool(poolRes.items || [])
    } catch (e: any) {
      setMessage(e?.message || '加入失败')
    } finally {
      setAdding(false)
    }
  }

  const removePoolItem = async (id: number) => {
    try {
      await strategyAnalysisApi.deletePoolItem(id)
      setPool((prev) => prev.filter((p) => p.id !== id))
    } catch (e: any) {
      setMessage(e?.message || '移除失败')
    }
  }

  const analyzeOne = async (item: StrategyPoolItem) => {
    if (!selectedId) {
      setMessage('请先选择一个策略')
      return null
    }
    setAnalyzingSymbol(keyOf(item.market, item.symbol))
    try {
      const res = await strategyAnalysisApi.analyze({
        strategy_id: selectedId,
        symbol: item.symbol,
        market: item.market,
        name: item.name,
      })
      setResults((prev) => ({ ...prev, [keyOf(item.market, item.symbol)]: res }))
      return res
    } catch (e: any) {
      setMessage(`${item.name || item.symbol} 分析失败：${e?.message || ''}`)
      return null
    } finally {
      setAnalyzingSymbol('')
    }
  }

  const analyzeAll = async () => {
    if (!selectedId) {
      setMessage('请先选择一个策略')
      return
    }
    if (!pool.length) {
      setMessage('策略池为空，请先加入股票')
      return
    }
    setAnalyzingAll(true)
    setMessage('开始逐票分析…')
    let ok = 0
    for (const item of pool) {
      const res = await analyzeOne(item)
      if (res) ok += 1
    }
    setAnalyzingAll(false)
    setMessage(`分析完成：成功 ${ok}/${pool.length}`)
  }

  return (
    <section className="card p-4">
      <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <div className="flex items-center gap-2 text-[15px] font-semibold">
            <Sparkles className="h-4 w-4 text-primary" />
            策略 AI 分析
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            把持仓或手动加入的股票放进策略池，让 AI 学习所选策略，并结合当天与最近行情逐票分析。
          </p>
        </div>
        <Button onClick={analyzeAll} disabled={analyzingAll || !pool.length || !selectedId}>
          {analyzingAll ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bot className="h-4 w-4" />}
          开始分析
        </Button>
      </div>

      {message && (
        <div className="mb-3 rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[12px] text-muted-foreground">
          {message}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
        {/* 左：策略 + 池子 */}
        <div className="space-y-4">
          <div className="rounded-lg border border-border/60 p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-[13px] font-semibold">分析策略</div>
              <Button variant="secondary" size="sm" className="h-7 px-2 text-[11px]" onClick={newStrategy}>
                <Plus className="h-3.5 w-3.5" /> 新建
              </Button>
            </div>
            <div className="mb-3 flex flex-wrap gap-1.5">
              {strategies.map((s) => (
                <button
                  key={s.id}
                  className={`rounded-full border px-2.5 py-1 text-[12px] transition-colors ${
                    selectedId === s.id
                      ? 'border-primary/50 bg-primary/10 text-primary'
                      : 'border-border/60 text-muted-foreground hover:border-primary/40'
                  }`}
                  onClick={() => applyStrategy(s)}
                >
                  {s.name}
                  {s.is_default ? ' ·默认' : ''}
                </button>
              ))}
              {strategies.length === 0 && (
                <span className="text-[12px] text-muted-foreground">暂无策略</span>
              )}
            </div>

            <label className="mb-1 block text-[11px] text-muted-foreground">策略名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} className="mb-2" />
            <label className="mb-1 block text-[11px] text-muted-foreground">说明</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} className="mb-2" />

            <div className="mb-1 flex items-center justify-between">
              <label className="text-[11px] text-muted-foreground">策略提示词（喂给 AI 的 system prompt）</label>
              <button className="text-[11px] text-primary" onClick={() => setEditingPrompt((v) => !v)}>
                {editingPrompt ? '收起' : '编辑'}
              </button>
            </div>
            {editingPrompt ? (
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                className="min-h-[220px] w-full resize-y rounded-lg border border-border bg-background px-3 py-2 font-mono text-[12px] text-foreground outline-none focus:border-primary/60"
                spellCheck={false}
              />
            ) : (
              <div className="max-h-[120px] overflow-hidden rounded-lg border border-border/50 bg-background/40 px-3 py-2 text-[11px] text-muted-foreground">
                {prompt ? `${prompt.slice(0, 280)}${prompt.length > 280 ? '…' : ''}` : '（空）'}
              </div>
            )}

            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" className="h-8" onClick={saveStrategy} disabled={saving || !dirty}>
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                保存策略
              </Button>
              {selected && !selected.is_default && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="h-8 text-destructive"
                  onClick={removeStrategy}
                  disabled={saving}
                >
                  <Trash2 className="h-3.5 w-3.5" /> 删除
                </Button>
              )}
            </div>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-[13px] font-semibold">策略池（{pool.length}）</div>
              <Button
                variant="secondary"
                size="sm"
                className="h-7 px-2 text-[11px]"
                onClick={importPositions}
                disabled={importing}
              >
                {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                导入持仓
              </Button>
            </div>
            <div className="mb-2 flex items-center gap-2">
              <select
                value={newMarket}
                onChange={(e) => setNewMarket(e.target.value)}
                className="h-9 rounded-lg border border-border bg-background px-2 text-[12px] outline-none"
              >
                <option value="CN">A股</option>
                <option value="HK">港股</option>
                <option value="US">美股</option>
              </select>
              <Input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                placeholder="代码，如 600519"
                onKeyDown={(e) => e.key === 'Enter' && addManual()}
                className="flex-1"
              />
              <Button size="sm" className="h-9" onClick={addManual} disabled={adding || !newSymbol.trim()}>
                {adding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              </Button>
            </div>
            <div className="space-y-1.5">
              {pool.map((p) => {
                const k = keyOf(p.market, p.symbol)
                const busy = analyzingSymbol === k
                return (
                  <div
                    key={p.id}
                    className="flex items-center justify-between rounded-lg border border-border/50 bg-background/40 px-2.5 py-1.5 text-[12px]"
                  >
                    <div className="min-w-0">
                      <span className="font-medium text-foreground">{p.name || p.symbol}</span>
                      <span className="ml-1 font-mono text-[11px] text-muted-foreground">{p.symbol}</span>
                      <span className="ml-1 text-[10px] text-muted-foreground">
                        {MARKET_LABEL[p.market] || p.market}
                        {p.source === 'position' ? ' ·持仓' : ''}
                      </span>
                    </div>
                    <div className="flex items-center gap-1">
                      {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />}
                      <button
                        className="text-muted-foreground hover:text-destructive"
                        onClick={() => removePoolItem(p.id)}
                        title="移除"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                )
              })}
              {pool.length === 0 && (
                <div className="rounded-lg border border-dashed border-border/60 px-3 py-6 text-center text-[12px] text-muted-foreground">
                  池子为空，点「导入持仓」或手动添加代码。
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 右：分析结果 */}
        <div className="rounded-lg border border-border/60">
          <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5">
            <div className="text-[13px] font-semibold">分析结果</div>
            <Button
              variant="secondary"
              size="sm"
              className="h-7 px-2"
              onClick={() => loadAll().catch(() => {})}
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="divide-y divide-border/50">
            {pool.map((p) => {
              const k = keyOf(p.market, p.symbol)
              const res = results[k]
              const isOpen = expanded === k
              return (
                <div key={p.id} className="px-4 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <button
                      className="flex min-w-0 flex-1 items-start gap-2 text-left"
                      onClick={() => setExpanded(isOpen ? null : k)}
                      disabled={!res}
                    >
                      <ChevronDown
                        className={`mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform ${
                          isOpen ? 'rotate-180' : ''
                        } ${res ? '' : 'opacity-30'}`}
                      />
                      <div className="min-w-0">
                        <div className="text-[13px] font-medium text-foreground">
                          {p.name || p.symbol}
                          <span className="ml-1.5 font-mono text-[11px] text-muted-foreground">{p.symbol}</span>
                        </div>
                        <div className="mt-0.5 line-clamp-2 text-[12px] text-muted-foreground">
                          {res ? res.verdict || '（无结论摘要，展开查看）' : '尚未分析'}
                        </div>
                      </div>
                    </button>
                    <Button
                      variant="secondary"
                      size="sm"
                      className="h-7 shrink-0 px-2 text-[11px]"
                      onClick={() => analyzeOne(p)}
                      disabled={analyzingSymbol === k || analyzingAll || !selectedId}
                    >
                      {analyzingSymbol === k ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Bot className="h-3.5 w-3.5" />
                      )}
                      {res ? '重测' : '分析'}
                    </Button>
                  </div>
                  {isOpen && res && (
                    <div className="prose prose-sm mt-3 max-w-none rounded-lg bg-background/40 px-3 py-2 text-[12px] text-foreground dark:prose-invert">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{res.content}</ReactMarkdown>
                      <div className="mt-2 text-[10px] text-muted-foreground">
                        {res.strategy_name} · {res.model} · {res.created_at}
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
            {pool.length === 0 && (
              <div className="px-4 py-12 text-center text-[12px] text-muted-foreground">
                先把股票加入策略池，再点右上角「开始分析」。
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
