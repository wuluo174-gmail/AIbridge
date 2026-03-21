<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import { PROMPT_KEYS } from '../lib/types.js'

  let { open = $bindable(false) }: { open: boolean } = $props()

  let values = $state<Record<string, string>>({})

  const LABELS: Record<string, { label: string; hint: string; rows: number }> = {
    claude_first: { label: 'Planner 初始方案提示词', hint: '变量: {task} {planner_name} — 第1轮，Planner 根据此提示生成方案', rows: 6 },
    claude_revise: { label: 'Planner 修订提示词', hint: '变量: {codex_feedback} {inject_section} — 第2+轮，Planner 根据反馈修订', rows: 6 },
    codex_first: { label: 'Reviewer 首次审查提示词', hint: '变量: {task} {claude_plan} — 第1轮，Reviewer 审查方案', rows: 6 },
    codex_review: { label: 'Reviewer 继续审查提示词', hint: '变量: {claude_revision} {inject_section} — 第2+轮，Reviewer 继续审查', rows: 6 },
    execution: { label: '执行提示词 (APPROVED)', hint: '变量: {task} {plan_section} — Reviewer APPROVED 后执行方案', rows: 4 },
    execution_unapproved: { label: '执行提示词 (未 APPROVED)', hint: '变量: {task} {plan_section} — 达到最大轮次但未 APPROVED 时执行', rows: 4 },
    codex_post_review: { label: '执行后审查提示词 (Exec Reviewer)', hint: '变量: {task} {approved_plan} {execution_result} {diff_section}', rows: 6 },
    claude_post_fix: { label: '修复提示词 (Executor)', hint: '变量: {review_feedback}', rows: 4 },
    codex_post_review_followup: { label: '再审查提示词 (Exec Reviewer)', hint: '变量: {fix_result} {diff_section}', rows: 4 },
    user_inject_label_claude: { label: '用户干预标签 (Planner)', hint: '注入用户意见时在 Planner 提示中显示的标题', rows: 1 },
    user_inject_label_codex: { label: '用户干预标签 (Reviewer)', hint: '注入用户意见时在 Reviewer 提示中显示的标题', rows: 1 },
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
    else alert(r.error ?? '保存失败')
  }

  export { onOpen as open_modal }
</script>

{#if open}
  <div class="modal-mask open">
    <div class="modal">
      <div class="modal-hdr">
        <span>提示词配置</span>
        <button class="close" onclick={onClose}>&times;</button>
      </div>
      <div class="modal-body">
        {#each PROMPT_KEYS as key}
          {@const meta = LABELS[key]}
          {#if meta}
            <div class="cfg-field">
              <!-- svelte-ignore a11y_label_has_associated_control -->
              <label>{meta.label}</label>
              <div class="cfg-hint">{meta.hint}</div>
              <textarea rows={meta.rows} bind:value={values[key]}></textarea>
            </div>
          {/if}
        {/each}
      </div>
      <div class="modal-foot">
        <button class="btn btn-cancel" onclick={onClose}>取消</button>
        <button class="btn btn-save" onclick={onSave}>保存</button>
      </div>
    </div>
  </div>
{/if}
