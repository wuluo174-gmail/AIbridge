import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineConfig } from '@playwright/test'


const port = Number(process.env.BRIDGE_E2E_PORT || '8876')
const frontendDir = path.dirname(fileURLToPath(import.meta.url))
const runtimeDir = path.join(os.tmpdir(), `bridge-e2e-${process.pid}`)
const dbPath = path.join(runtimeDir, 'bridge.db')
const logDir = path.join(runtimeDir, 'logs')

fs.mkdirSync(logDir, { recursive: true })

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    viewport: { width: 1280, height: 900 },
  },
  webServer: {
    cwd: frontendDir,
    command: `npm run build && python3 ../bridge.py --port ${port} --no-browser --enable-fixture-tools --db-path ${dbPath} --log-dir ${logDir}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    timeout: 60_000,
  },
})
