<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  let plannerId = $state(store.roleConfig.planner_tool_id)
  let reviewerId = $state(store.roleConfig.reviewer_tool_id)

  function onChange() {
    store.onRoleChange(plannerId, reviewerId)
  }

  // Sync from store when initRoleConfig refreshes
  $effect(() => {
    plannerId = store.roleConfig.planner_tool_id
    reviewerId = store.roleConfig.reviewer_tool_id
  })
</script>

<div class="field f-role">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>Planner</label>
  <select class="role-select" bind:value={plannerId} onchange={onChange}>
    {#each Object.values(store.toolMap) as t}
      <option value={t.id}>
        {t.display_name}{t.detected_installed === false ? '（启动扫描未发现）' : ''}
      </option>
    {/each}
  </select>
</div>
<div class="field f-role">
  <!-- svelte-ignore a11y_label_has_associated_control -->
  <label>Reviewer</label>
  <select class="role-select" bind:value={reviewerId} onchange={onChange}>
    {#each Object.values(store.toolMap) as t}
      <option value={t.id}>
        {t.display_name}{t.detected_installed === false ? '（启动扫描未发现）' : ''}
      </option>
    {/each}
  </select>
</div>
{#if store.execNote}
  <span class="exec-note">{store.execNote}</span>
{/if}
