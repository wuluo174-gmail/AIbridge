<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import LogView from './LogView.svelte'
  import VersionView from './VersionView.svelte'
  import type { AgentPanel } from '../lib/types.js'

  let { agent }: { agent: AgentPanel } = $props()

  let dotClass = $derived(agent === 'planner' ? 'dot dot-planner' : 'dot dot-reviewer')
  let displayName = $derived(store.toolDisplayNames[agent])
  let logActive = $derived(store.activeTab[agent] === 'log')
  let resultActive = $derived(store.activeTab[agent] === 'result')
</script>

<div class="panel">
  <div class="panel-head" id="head_{agent}">
    <span class={dotClass}></span>
    <span class="panel-tool-name">{displayName}</span>
    {#if store.doneBadge && agent === store.executorPanel}
      <span class="done-badge">{store.t(store.doneBadge)}</span>
    {/if}
    <div class="tab-group">
      <button class="tab" class:active={logActive} onclick={() => store.switchTab(agent, 'log')}>{store.t('panel.log')}</button>
      <button class="tab" class:active={resultActive} onclick={() => store.switchTab(agent, 'result')}>{store.t('panel.result')}</button>
    </div>
  </div>
  <div class="tab-body">
    <LogView entries={store.logs[agent]} active={logActive} />
    <VersionView {agent} active={resultActive} />
  </div>
</div>
