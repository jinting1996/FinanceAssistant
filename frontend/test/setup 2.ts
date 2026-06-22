// 测试环境通用初始化：注入一个内存版 localStorage，
// 避免 jsdom 不同版本下 Storage 行为差异导致的不稳定。
class MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length() {
    return this.store.size
  }
  clear() {
    this.store.clear()
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null
  }
  removeItem(key: string) {
    this.store.delete(key)
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value))
  }
}

Object.defineProperty(globalThis, 'localStorage', {
  value: new MemoryStorage(),
  writable: true,
  configurable: true,
})

// jsdom 不支持真实导航；logout() 会赋值 window.location.href，
// 用可写的桩替换，避免测试输出里刷 "Not implemented: navigation" 噪音。
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'location', {
    value: { href: 'http://localhost/' },
    writable: true,
    configurable: true,
  })
}
