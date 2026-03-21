<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import { showAlert } from '../lib/dialog.svelte.js'
  import { PROMPT_KEYS } from '../lib/types.js'

  let { open = $bindable(false) }: { open: boolean } = $props()

  let values = $state<Record<string, string>>({})

  const ROWS: Record<string, number> = {
    claude_first: 6, claude_revise: 6, codex_first: 6, codex_review: 6,
    execution: 4, execution_unapproved: 4,
    codex_post_review: 6, claude_post_fix: 4, codex_post_review_followup: 4,
    user_inject_label_claude: 1, user_inject_label_codex: 1,
  }

  async function onOpen() {
    const data = await store.loadPrompts()
    values = { ...data }
    open = true
  }

  function onClose() {
    open = false
  }

  async function onSave() {
    const body: Record<string, string> = {}
    for (const k of PROMPT_KEYS) body[k] = values[k] ?? ''
    const r = await store.savePrompts(body)
    if (r.ok) onClose()
    else await showAlert(r.error ?? store.t('prompt.save_fail'))
  }

  export { onOpen as open_modal }
</script>

{#if open}
  <div class="modal-mask open">
    <div class="modal">
      <div class="modal-hdr">
        <span>{store.t('prompt.title')}</span>
        <button class="close" onclick={onClose}>&times;</button>
      </div>
      <div class="modal-body">
        {#each PROMPT_KEYS as key}
          <div class="cfg-field">
            <!-- svelte-ignore a11y_label_has_associated_control -->
            <label>{store.t(`prompt.${key}.label`)}</label>
            <div class="cfg-hint">{store.t(`prompt.${key}.hint`)}</div>
            <textarea rows={ROWS[key] ?? 4} bind:value={values[key]}></textarea>
          </div>
        {/each}
      </div>
      <div class="modal-foot">
        <button class="btn btn-cancel" onclick={onClose}>{store.t('common.cancel')}</button>
        <button class="btn btn-save" onclick={onSave}>{store.t('common.save')}</button>
      </div>
    </div>
  </div>
{/if}
