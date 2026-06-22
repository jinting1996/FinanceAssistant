import { defineConfig } from 'vitest/config'
import path from 'path'

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@panwatch/api': path.resolve(__dirname, './packages/api/src'),
      '@panwatch/base-ui': path.resolve(__dirname, './packages/base-ui/src'),
      '@panwatch/biz-ui': path.resolve(__dirname, './packages/biz-ui/src'),
    },
  },
  test: {
    environment: 'jsdom',
    // jsdom 默认 about:blank 为 opaque origin，localStorage 不可用；指定 url 后才生效
    environmentOptions: { jsdom: { url: 'http://localhost' } },
    globals: true,
    setupFiles: ['./test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}', 'packages/**/*.{test,spec}.{ts,tsx}'],
  },
})
