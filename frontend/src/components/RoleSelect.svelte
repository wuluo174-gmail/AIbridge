<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  let plannerId = $state(store.defaultRoleConfig.planner_tool_id)
  let reviewerId = $state(store.defaultRoleConfig.reviewer_tool_id)
  let saving = $state(false)

  const tools = $derived(Object.values(store.toolMap))
  const dirty = $derived(
    plannerId !== store.defaultRoleConfig.planner_tool_id
      || reviewerId !== store.defaultRoleConfig.reviewer_tool_id
  )

  async function applyRoles() {
    if (!dirty || saving) return
    saving = true
    try {
      await store.onRoleChange(plannerId, reviewerId)
    } finally {
      saving = false
    }
  }

  // Sync from store when initRoleConfig refreshes
  $effect(() => {
    plannerId = store.defaultRoleConfig.planner_tool_id
    reviewerId = store.defaultRoleConfig.reviewer_tool_id
  })
</script>

<div class="field f-role">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>{store.t('role.planner')}</label>
  <select class="role-select" bind:value={plannerId}>
    {#each tools as tool}
      <option value={tool.id}>
        {tool.display_name}{tool.detected_installed === false ? store.t('role.not_found') : ''}
      </option>
    {/each}
  </select>
</div>
<div class="field f-role">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>{store.t('role.reviewer')}</label>
  <select class="role-select" bind:value={reviewerId}>
    {#each tools as tool}
      <option value={tool.id}>
        {tool.display_name}{tool.detected_installed === false ? store.t('role.not_found') : ''}
      </option>
    {/each}
  </select>
</div>
<div class="field f-role-actions">
  <span class="role-actions-label">{store.t('common.apply')}</span>
  <div class="role-actions">
    <button class="btn btn-cfg role-apply-btn" disabled={!dirty || saving} onclick={applyRoles}>
      {store.t('common.apply')}
    </button>
    <span class="role-hint">{store.t('role.apply_hint')}</span>
  </div>
</div>
