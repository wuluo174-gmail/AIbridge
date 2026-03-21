import { describe, it, expect } from 'vitest'
import { showConfirm, showAlert, dialog } from '../lib/dialog.svelte.js'

describe('dialog state module', () => {
  it('initial state is closed', () => {
    expect(dialog.open).toBe(false)
    expect(dialog.message).toBe('')
    expect(dialog.mode).toBe('confirm')
  })

  it('showConfirm opens dialog and sets mode=confirm', () => {
    const _p = showConfirm('are you sure?')
    expect(dialog.open).toBe(true)
    expect(dialog.message).toBe('are you sure?')
    expect(dialog.mode).toBe('confirm')
    dialog.resolve(false)
  })

  it('showConfirm resolves true when dialog.resolve(true)', async () => {
    const p = showConfirm('confirm this')
    dialog.resolve(true)
    expect(await p).toBe(true)
    expect(dialog.open).toBe(false)
  })

  it('showConfirm resolves false when dialog.resolve(false)', async () => {
    const p = showConfirm('confirm this')
    dialog.resolve(false)
    expect(await p).toBe(false)
    expect(dialog.open).toBe(false)
  })

  it('showAlert opens dialog with mode=alert', () => {
    const _p = showAlert('error occurred')
    expect(dialog.open).toBe(true)
    expect(dialog.message).toBe('error occurred')
    expect(dialog.mode).toBe('alert')
    dialog.resolve(true)
  })

  it('showAlert resolves void regardless of resolve arg', async () => {
    const p = showAlert('info')
    dialog.resolve(false)
    await expect(p).resolves.toBeUndefined()
    expect(dialog.open).toBe(false)
  })

  it('resolve without pending promise is a no-op', () => {
    dialog.resolve(true)
    expect(dialog.open).toBe(false)
  })

  it('sequential showConfirm dismisses previous with false', async () => {
    const p1 = showConfirm('first')
    const p2 = showConfirm('second')
    expect(dialog.message).toBe('second')
    expect(await p1).toBe(false)
    dialog.resolve(true)
    expect(await p2).toBe(true)
  })
})
