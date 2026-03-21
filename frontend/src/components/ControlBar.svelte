<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import PathInput from './PathInput.svelte'
  import RoleSelect from './RoleSelect.svelte'
  import StatusPill from './StatusPill.svelte'

  let { onBrowse, onOpenPrompts, onOpenHistory }: {
    onBrowse: () => void
    onOpenPrompts: () => void
    onOpenHistory: () => void
  } = $props()
</script>

<div class="controls">
  <div class="controls-config">
    <PathInput bind:value={store.projectPath} {onBrowse} />
    <div class="field f-task">
      <!-- svelte-ignore a11y_label_has_associated_control -->
      <label>{store.t('ctrl.task')}</label>
      <textarea rows="3" placeholder={store.t('ctrl.task_ph')} bind:value={store.taskValue}></textarea>
    </div>
    <div class="field f-rounds">
      <!-- svelte-ignore a11y_label_has_associated_control -->
      <label>{store.t('ctrl.rounds')}</label>
      <input type="number" bind:value={store.roundsValue} min="1" max="20" />
    </div>
    <RoleSelect />
  </div>
  <div class="controls-status">
    {#if store.execNote}
      <span class="exec-note">{store.execNote}</span>
    {/if}
    <StatusPill />
  </div>
  <div class="controls-actions">
    <button class="btn btn-go" disabled={!store.canStart} onclick={() => store.doStart(store.projectPath, store.taskValue, store.roundsValue)}>{store.t('ctrl.start')}</button>
    <button class="btn btn-stop" disabled={!store.canStop} onclick={() => store.doStop()}>{store.t('ctrl.stop')}</button>
    <button class="btn btn-exec" disabled={!store.canExecute} onclick={() => store.doExec()}>{store.t('ctrl.exec')}</button>
    {#if store.canContinue}
      <button class="btn btn-cont" onclick={() => store.doContinue(store.extraRounds)}>{store.t('ctrl.continue')}</button>
      <input type="number" bind:value={store.extraRounds} min="1" max="20"
        title={store.t('ctrl.extra_rounds')} style="width:50px;text-align:center;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px;font-size:13px" />
    {/if}
    {#if store.canFix}
      <button class="btn btn-fix" onclick={() => store.doReviewFix()}>{store.t('ctrl.fix')}</button>
      <button class="btn btn-skip" onclick={() => store.doReviewSkip()}>{store.t('ctrl.skip')}</button>
    {/if}
    {#if store.canReviewContinue}
      <button class="btn btn-cont" onclick={() => store.doReviewContinue(store.extraRounds)}>{store.t('ctrl.review_continue')}</button>
      <button class="btn btn-skip" onclick={() => store.doReviewSkip()}>{store.t('ctrl.skip')}</button>
      <input type="number" bind:value={store.extraRounds} min="1" max="20"
        title={store.t('ctrl.extra_rounds')} style="width:50px;text-align:center;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px;font-size:13px" />
    {/if}
    <button class="btn btn-cfg" onclick={() => store.toggleTheme()}>{store.theme === 'dark' ? '☀' : '🌙'}</button>
    <button class="btn btn-cfg" onclick={() => store.switchLocale()}>{store.getLocale() === 'zh-CN' ? 'EN' : '中'}</button>
    <button class="btn btn-cfg" onclick={onOpenHistory}>{store.t('ctrl.history')}</button>
    <button class="btn btn-cfg" onclick={onOpenPrompts}>{store.t('ctrl.prompts')}</button>
  </div>
</div>
