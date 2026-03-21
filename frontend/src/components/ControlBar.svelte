<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import PathInput from './PathInput.svelte'
  import RoleSelect from './RoleSelect.svelte'
  import StatusPill from './StatusPill.svelte'

  let { projectPath = $bindable(''), onBrowse, onOpenPrompts }: {
    projectPath: string
    onBrowse: () => void
    onOpenPrompts: () => void
  } = $props()

  let taskValue = $state('')
  let roundsValue = $state(5)
  let extraRounds = $state(3)
</script>

<div class="controls">
  <PathInput bind:value={projectPath} {onBrowse} />
  <div class="field f-task">
    <!-- svelte-ignore a11y_label_has_associated_control -->
    <label>任务描述</label>
    <textarea rows="3" placeholder="描述任务..." bind:value={taskValue}></textarea>
  </div>
  <div class="field f-rounds">
    <!-- svelte-ignore a11y_label_has_associated_control -->
    <label>轮次</label>
    <input type="number" bind:value={roundsValue} min="1" max="20" />
  </div>
  <RoleSelect />
  <button class="btn btn-go" disabled={!store.canStart} onclick={() => store.doStart(projectPath, taskValue, roundsValue)}>▶ 开始</button>
  <button class="btn btn-stop" disabled={!store.canStop} onclick={() => store.doStop()}>⏹ 中止</button>
  <button class="btn btn-exec" disabled={!store.canExecute} onclick={() => store.doExec()}>⚡ 执行</button>
  {#if store.canContinue}
    <button class="btn btn-cont" onclick={() => store.doContinue(extraRounds)}>继续协商</button>
    <input type="number" bind:value={extraRounds} min="1" max="20"
      title="额外轮次" style="width:50px;text-align:center;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:6px;font-size:13px" />
  {/if}
  {#if store.canFix}
    <button class="btn btn-fix" onclick={() => store.doReviewFix()}>🔧 确认修复</button>
    <button class="btn btn-skip" onclick={() => store.doReviewSkip()}>⏭ 跳过修复</button>
  {/if}
  <button class="btn btn-cfg" onclick={onOpenPrompts}>⚙ 提示词</button>
  <StatusPill />
</div>
