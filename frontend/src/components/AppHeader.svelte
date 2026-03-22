<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  let { onOpenPrompts, onOpenHistory }: {
    onOpenPrompts: () => void
    onOpenHistory: () => void
  } = $props()
</script>

<div class="app-header">
  <div class="app-brand">
    <span class="app-title">{store.t('app.title')}</span>
    <span class="app-subtitle">Planner / Reviewer Workspace</span>
  </div>

  <div class="app-actions">
    <button class="btn btn-cfg" onclick={() => store.toggleTheme()}>
      {store.theme === 'dark' ? '☀ ' : '🌙 '}{store.t('app.theme')}
    </button>

    <div class="locale-switch" aria-label={store.t('app.language')}>
      <button
        class="locale-btn"
        class:active={store.getLocale() === 'zh-CN'}
        onclick={() => store.setLocale('zh-CN')}
      >
        中文
      </button>
      <button
        class="locale-btn"
        class:active={store.getLocale() === 'en-US'}
        onclick={() => store.setLocale('en-US')}
      >
        EN
      </button>
    </div>

    <button class="btn btn-cfg" onclick={onOpenHistory}>{store.t('app.history')}</button>
    <button class="btn btn-cfg" onclick={onOpenPrompts}>{store.t('app.prompts')}</button>
  </div>
</div>
