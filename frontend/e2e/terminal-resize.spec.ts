import { expect, test } from '@playwright/test'


const PROJECT_PATH = '/Users/809456948qq.com/code/bridge'
const ROLE_KEYS = ['planner', 'reviewer', 'executor', 'validator'] as const

function extractViewportLabel(text: string | null): string {
  const match = text?.match(/(\d+)x(\d+)/)
  return match ? `${match[1]}x${match[2]}` : ''
}

test('terminal resize flows through API, SSE, and persisted lane viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1400 })
  await page.goto('/')

  await page.getByTestId('project-path-input').fill(PROJECT_PATH)
  await page.getByTestId('task-input').fill('验证 terminal resize 会进入 lane viewport 真相源')
  await page.getByTestId('view-mode-select').selectOption('terminal')

  for (const roleKey of ROLE_KEYS) {
    await expect(page.locator(`[data-testid="role-tool-${roleKey}"] option[value="fixture-cli"]`)).toHaveCount(1)
    await page.getByTestId(`role-tool-${roleKey}`).selectOption('fixture-cli')
  }

  await page.getByTestId('start-session').click()

  await expect(page.getByTestId('current-view-mode')).toHaveText('terminal')
  await expect(page.getByTestId('role-pane-planner')).toBeVisible()

  const plannerMeta = page.getByTestId('role-meta-planner')
  await page.setViewportSize({ width: 1180, height: 1200 })
  await expect
    .poll(async () => extractViewportLabel(await plannerMeta.textContent()), {
      message: '浏览器第一次 resize 后，planner lane 应显示来自 /api/terminal/resize 的 viewport 尺寸',
    })
    .not.toBe('')
  const before = extractViewportLabel(await plannerMeta.textContent())

  await page.setViewportSize({ width: 960, height: 1080 })

  await expect
    .poll(async () => {
      const next = extractViewportLabel(await plannerMeta.textContent())
      return next && next !== before ? next : ''
    }, {
      message: '浏览器 resize 后，planner lane 的 viewport 标签应通过 /api/terminal/resize + SSE 更新',
    })
    .not.toBe('')
  const after = extractViewportLabel(await plannerMeta.textContent())

  const sessionId = (await page.getByTestId('current-session-id').textContent())?.trim() || ''
  expect(sessionId).not.toBe('')

  const historySnapshot = await page.evaluate(async (sid) => {
    const response = await fetch(`/api/history?sid=${sid}`)
    const payload = await response.json()
    const planner = payload.roles.find((role: { role_key: string; viewport?: { cols: number; rows: number } }) => role.role_key === 'planner')
    return {
      viewport: planner?.viewport ? `${planner.viewport.cols}x${planner.viewport.rows}` : '',
      viewportEvents: payload.events.filter((event: { type: string; role_key: string | null }) => event.type === 'lane.viewport_changed' && event.role_key === 'planner').length,
    }
  }, sessionId)

  expect(historySnapshot.viewport).toBe(after)
  expect(historySnapshot.viewportEvents).toBeGreaterThan(0)
})
