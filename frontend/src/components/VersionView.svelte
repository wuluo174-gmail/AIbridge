<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import type { AgentPanel } from '../lib/types.js'

  let { agent, active }: { agent: AgentPanel; active: boolean } = $props()

  let vers = $derived(store.versions[agent])
  let hasExecResult = $derived(agent === store.executorPanel && store.executionResult != null)
  let idx = $derived(store.activeVer[agent] < 0 ? vers.length - 1 : Math.min(store.activeVer[agent], vers.length - 1))

  function onTabClick(i: number) {
    store.selectVersion(agent, i)
  }
</script>

<div class="tab-pane" class:active>
  <div class="result-wrap">
    <div class="ver-bar">
      {#each vers as ver, i}
        <button
          class="ver-tab"
          class:active={(!store.showExecResult || agent !== store.executorPanel) && i === idx}
          onclick={() => onTabClick(i)}
        >v{i + 1} (R{ver.round})</button>
      {/each}
      {#if hasExecResult}
        <button
          class="ver-tab vt-exec"
          class:active={store.showExecResult}
          onclick={() => onTabClick(-2)}
        >执行结果</button>
      {/if}
    </div>
    <div class="ver-content">
      {#if agent === store.executorPanel && store.showExecResult && store.executionResult != null}
        <span class="ok">── 执行结果 ──</span>
{store.executionResult}
      {:else if vers.length && vers[idx]}
        <span class="ok">── R{vers[idx].round} {vers[idx].phase} ──</span>
{vers[idx].content}
      {/if}
    </div>
  </div>
</div>
