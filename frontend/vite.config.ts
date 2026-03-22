import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8686',
    },
  },
  test: {
    include: ['src/**/*.test.ts'],
    exclude: ['e2e/**'],
  },
})
