<script lang="ts">
  import type { LogEntry } from '../lib/types.js'

  let { entries, active }: { entries: LogEntry[]; active: boolean } = $props()

  // Group fold_start/fold_chunk/fold_end into renderable fold blocks
  interface FoldBlock { label: string; chunks: string[]; open: boolean }

  let renderItems = $derived.by(() => {
    const items: (LogEntry | { kind: 'fold_block'; fold: FoldBlock })[] = []
    let currentFold: FoldBlock | null = null

    for (const entry of entries) {
      if (entry.kind === 'fold_start') {
        currentFold = { label: entry.label, chunks: [], open: true }
      } else if (entry.kind === 'fold_chunk') {
        if (currentFold) currentFold.chunks.push(entry.text)
        else items.push(entry)
      } else if (entry.kind === 'fold_end') {
        if (currentFold) {
          currentFold.open = false
          items.push({ kind: 'fold_block', fold: currentFold })
          currentFold = null
        }
      } else {
        items.push(entry)
      }
    }
    // If fold is still open (no fold_end yet), render it as open
    if (currentFold) items.push({ kind: 'fold_block', fold: currentFold })
    return items
  })

  let el: HTMLDivElement | undefined = $state()

  $effect(() => {
    void entries.length
    if (el) el.scrollTop = el.scrollHeight
  })
</script>

<div class="term tab-pane" class:active bind:this={el}>
  {#each renderItems as item}
    {#if item.kind === 'text'}
      {item.text}
    {:else if item.kind === 'command'}
      <span class="chunk-cmd">{item.text}</span>
    {:else if item.kind === 'separator'}
      <div class="log-sep {item.level}">{item.text}</div>
    {:else if item.kind === 'mcp'}
      <span class="mcp-line">[MCP] {item.text}</span>
    {:else if item.kind === 'fold_block'}
      <details class="chunk-fold" open={item.fold.open}>
        <summary>{item.fold.label}</summary>
        <div class="fold-body">{item.fold.chunks.join('')}</div>
      </details>
    {:else if item.kind === 'collapsible'}
      <details class="plan-preview" open={item.open}>
        <summary>{item.label}</summary>
        <div class="plan-body">{item.content}</div>
      </details>
    {/if}
  {/each}
</div>
