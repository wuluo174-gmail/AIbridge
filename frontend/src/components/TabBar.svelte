<script lang="ts">
  import { store } from '../lib/store.svelte.js'
</script>

<div class="tab-bar">
  {#each store.tabs as tab (tab.id)}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="session-tab" class:active={tab.id === store.activeTabId}
      onclick={() => store.switchToTab(tab.id)}
      onkeydown={(e) => { if (e.key === 'Enter') store.switchToTab(tab.id) }}
      role="tab" tabindex="0">
      <span class="tab-label">{tab.label}</span>
      {#if store.tabs.length > 1}
        <button type="button" class="tab-close"
          onclick={(e) => { e.stopPropagation(); store.closeTab(tab.id) }}>×</button>
      {/if}
    </div>
  {/each}
  <button class="session-tab tab-new" onclick={() => store.newTab()}>+</button>
</div>
