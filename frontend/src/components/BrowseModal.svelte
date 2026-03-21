<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import { showAlert } from '../lib/dialog.svelte.js'

  let { open = $bindable(false), onSelect }: { open: boolean; onSelect: (path: string) => void } = $props()

  let browseCurrent = $state('')
  let pathInput = $state('')
  let dirs = $state<{ path: string; name: string; is_git: boolean }[]>([])
  let parent = $state<string | null>(null)
  let info = $state('')
  let selectedPath = $state('')
  let clickTimer: ReturnType<typeof setTimeout> | null = null

  export async function openAt(initialPath: string) {
    open = true
    await browseDir(initialPath || '')
  }

  async function browseDir(path: string) {
    const r = await store.browseDir(path)
    if (r.error) { await showAlert(r.error); return }
    browseCurrent = r.current
    selectedPath = r.current
    pathInput = r.current
    parent = r.parent
    dirs = r.dirs
    info = r.current + (r.is_git ? '  ' + store.t('browse.git_tag') : '') + (r.truncated ? '  ' + store.t('browse.truncated') : '')
  }

  function onClose() { open = false }

  function onSelectClick() {
    onSelect(selectedPath)
    open = false
  }

  function onGoClick() {
    if (pathInput.trim()) browseDir(pathInput.trim())
  }

  function onPathKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && pathInput.trim()) browseDir(pathInput.trim())
  }

  function onItemClick(path: string, isParent: boolean) {
    if (isParent) { browseDir(path); return }
    if (clickTimer) clearTimeout(clickTimer)
    clickTimer = setTimeout(() => {
      selectedPath = path
      info = path
    }, 200)
  }

  function onItemDblClick(path: string) {
    if (clickTimer) clearTimeout(clickTimer)
    browseDir(path)
  }
</script>

{#if open}
  <div class="modal-mask open">
    <div class="modal">
      <div class="modal-hdr">
        <span>{store.t('browse.title')}</span>
        <button class="close" onclick={onClose}>&times;</button>
      </div>
      <div class="modal-body" style="padding:0">
        <div class="browse-bar">
          <input type="text" bind:value={pathInput} placeholder={store.t('browse.path_ph')}
            onkeydown={onPathKeydown} />
          <button class="btn" onclick={onGoClick}>{store.t('browse.go')}</button>
        </div>
        <div style="overflow-y:auto;max-height:55vh">
          {#if parent}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="browse-item bi-parent" onclick={() => onItemClick(parent!, true)}>
              <span class="bi-icon">⬆</span><span class="bi-name">..</span>
            </div>
          {/if}
          {#each dirs as d}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="browse-item" class:selected={selectedPath === d.path}
              onclick={() => onItemClick(d.path, false)}
              ondblclick={() => onItemDblClick(d.path)}>
              <span class="bi-icon">📁</span>
              <span class="bi-name">{d.name}</span>
              {#if d.is_git}
                <span class="bi-git">GIT</span>
              {/if}
            </div>
          {/each}
          {#if !dirs.length && parent}
            <div style="padding:20px;text-align:center;color:var(--dim)">{store.t('browse.empty')}</div>
          {/if}
        </div>
      </div>
      <div class="modal-foot">
        <div style="flex:1;font-size:12px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          {info}
        </div>
        <button class="btn btn-cancel" onclick={onClose}>{store.t('common.cancel')}</button>
        <button class="btn btn-save" onclick={onSelectClick}>{store.t('browse.select')}</button>
      </div>
    </div>
  </div>
{/if}
