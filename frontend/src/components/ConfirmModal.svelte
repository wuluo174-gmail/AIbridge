<script lang="ts">
  import { dialog } from '../lib/dialog.svelte.js'
  import { store } from '../lib/store.svelte.js'

  let confirmBtn: HTMLButtonElement | undefined = $state()
  let cancelBtn: HTMLButtonElement | undefined = $state()

  $effect(() => {
    if (dialog.open) confirmBtn?.focus()
  })

  function onConfirm() { dialog.resolve(true) }
  function onCancel() { dialog.resolve(false) }

  function onKeydown(e: KeyboardEvent) {
    if (!dialog.open) return
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onCancel() }
    else if (e.key === 'Tab') {
      e.preventDefault()
      e.stopPropagation()
      if (dialog.mode === 'confirm' && cancelBtn && confirmBtn) {
        const target = document.activeElement === cancelBtn ? confirmBtn : cancelBtn
        target.focus()
      }
    }
  }

  function onMaskClick(e: MouseEvent) {
    if (e.target === e.currentTarget) onCancel()
  }
</script>

{#if dialog.open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="confirm-mask" onkeydown={onKeydown} onclick={onMaskClick}>
    <div class="confirm-box" role="alertdialog" aria-modal="true">
      <div class="confirm-msg">{dialog.message}</div>
      <div class="confirm-actions">
        {#if dialog.mode === 'confirm'}
          <button class="btn btn-cancel" bind:this={cancelBtn} onclick={onCancel}>{store.t('common.cancel')}</button>
        {/if}
        <button class="btn btn-save" bind:this={confirmBtn} onclick={onConfirm}>{store.t('common.ok')}</button>
      </div>
    </div>
  </div>
{/if}
