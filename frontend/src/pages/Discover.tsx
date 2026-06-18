import { useState } from 'react'
import { SlidersHorizontal, Sparkles } from 'lucide-react'
import ScreenerPage from '@/pages/Screener'
import OpportunitiesPage from '@/pages/Opportunities'

type DiscoverTab = 'screener' | 'opportunities'

const TABS: { key: DiscoverTab; label: string; icon: typeof SlidersHorizontal }[] = [
  { key: 'screener', label: '选股', icon: SlidersHorizontal },
  { key: 'opportunities', label: '机会', icon: Sparkles },
]

// 「发现」聚合页:统一入口,内部 Tab 切换「我的公式筛选(选股)/ 系统候选(机会)」。
// 直接复用两个现有页面组件,不改其内部逻辑。
export default function DiscoverPage({ initialTab = 'screener' }: { initialTab?: DiscoverTab }) {
  const [tab, setTab] = useState<DiscoverTab>(initialTab)
  return (
    <div>
      <div className="mb-4">
        <div className="inline-flex items-center gap-1 p-1 rounded-lg bg-accent/30">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] transition-colors ${
                tab === key ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'screener' ? <ScreenerPage /> : <OpportunitiesPage />}
    </div>
  )
}
