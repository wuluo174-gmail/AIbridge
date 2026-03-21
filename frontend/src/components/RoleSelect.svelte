<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  let plannerId = $state(store.defaultRoleConfig.planner_tool_id)
  let reviewerId = $state(store.defaultRoleConfig.reviewer_tool_id)

  function onChange() {
    store.onRoleChange(plannerId, reviewerId)
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
  <select class="role-select" bind:value={plannerId} onchange={onChange}>
    {#each Object.values(store.toolMap) as tool}
      <option value={tool.id}>
        {tool.display_name}{tool.detected_installed === false ? store.t('role.not_found') : ''}
      </option>
    {/each}
  </select>
</div>
<div class="field f-role">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>{store.t('role.reviewer')}</label>
  <select class="role-select" bind:value={reviewerId} onchange={onChange}>
    {#each Object.values(store.toolMap) as tool}
      <option value={tool.id}>
        {tool.display_name}{tool.detected_installed === false ? store.t('role.not_found') : ''}
      </option>
    {/each}
  </select>
</div>
