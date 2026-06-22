import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchAPI, isAuthenticated } from './client'

function mockResponse(body: unknown, status = 200): Response {
  return {
    status,
    json: async () => body,
  } as unknown as Response
}

describe('fetchAPI', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('解包成功响应的 data 字段', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockResponse({ code: 0, data: { value: 42 }, message: 'ok' }),
    ))
    const data = await fetchAPI<{ value: number }>('/foo')
    expect(data).toEqual({ value: 42 })
  })

  it('附带 Bearer token（存在时）', async () => {
    localStorage.setItem('token', 'abc')
    const fetchMock = vi.fn().mockResolvedValue(
      mockResponse({ code: 0, data: null, message: 'ok' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await fetchAPI('/foo')
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>
    expect(headers['Authorization']).toBe('Bearer abc')
  })

  it('业务 code 非 0 时抛出 message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockResponse({ code: 1, data: null, message: '余额不足' }),
    ))
    await expect(fetchAPI('/foo')).rejects.toThrow('余额不足')
  })

  it('success === false 时抛错', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      mockResponse({ code: 0, success: false, data: null, message: '失败' }),
    ))
    await expect(fetchAPI('/foo')).rejects.toThrow('失败')
  })

  it('401 时清除 token 并抛出登录过期', async () => {
    localStorage.setItem('token', 'abc')
    // jsdom 不允许真正跳转，stub 掉 location.href 赋值
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse({}, 401)))
    await expect(fetchAPI('/foo')).rejects.toThrow('登录已过期')
    expect(localStorage.getItem('token')).toBeNull()
  })
})

describe('isAuthenticated', () => {
  beforeEach(() => localStorage.clear())

  it('无 token 时返回 false', () => {
    expect(isAuthenticated()).toBe(false)
  })

  it('token 已过期时返回 false', () => {
    localStorage.setItem('token', 'abc')
    localStorage.setItem('token_expires', new Date(Date.now() - 1000).toISOString())
    expect(isAuthenticated()).toBe(false)
  })

  it('token 未过期时返回 true', () => {
    localStorage.setItem('token', 'abc')
    localStorage.setItem('token_expires', new Date(Date.now() + 60_000).toISOString())
    expect(isAuthenticated()).toBe(true)
  })
})
