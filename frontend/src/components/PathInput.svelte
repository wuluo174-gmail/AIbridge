<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  let { value = $bindable(''), onBrowse }: { value: string; onBrowse: () => void } = $props()

  let dropdownOpen = $state(false)
  let items = $state<{ path: string; name: string; is_git?: boolean; section?: string }[]>([])
  let acTimer: ReturnType<typeof setTimeout> | null = null

  function onInput() {
    if (acTimer) clearTimeout(acTimer)
    acTimer = setTimeout(doAutoComplete, 200)
  }

  async function onFocus() {
    if (value.trim()) return
    const r = await store.recentPaths()
    if (!r.paths?.length) { dropdownOpen = false; return }
    items = r.paths.map((p, i) => ({ path: p, name: p, section: i === 0 ? store.t('path.recent') : undefined }))
    dropdownOpen = true
  }

  function onBlur() {
    setTimeout(() => { dropdownOpen = false }, 200)
  }

  async function doAutoComplete() {
    const prefix = value.trim()
    if (!prefix) { await onFocus(); return }
    const r = await store.completePath(prefix)
    if (!r.suggestions?.length) { dropdownOpen = false; return }
    items = r.suggestions.map(s => ({ path: s.path, name: s.name, is_git: s.is_git }))
    dropdownOpen = true
  }

  function pick(path: string) {
    value = path
    dropdownOpen = false
  }
</script>

<div class="field f-path">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>{store.t('path.label')}</label>
  <div class="path-wrap">
    <input type="text" bind:value autocomplete="off" placeholder={store.t('path.ph')}
      oninput={onInput} onfocus={onFocus} onblur={onBlur} />
    <button class="btn btn-browse" title={store.t('path.browse')} onclick={onBrowse}>📂</button>
    <div class="path-dropdown" class:open={dropdownOpen}>
      {#each items as item}
        {#if item.section}
          <div class="pd-section">{item.section}</div>
        {/if}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pd-item" onmousedown={(e) => { e.preventDefault(); pick(item.path) }}>
          {item.name}
          {#if item.is_git}
            <span class="pd-git">GIT</span>
          {/if}
        </div>
      {/each}
    </div>
  </div>
</div>
