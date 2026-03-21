<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  const STATUS_LABELS: Record<string, string> = {
    idle: 'IDLE', running: 'NEGOTIATING', consensus: 'CONSENSUS',
    max_rounds: 'MAX ROUNDS', executing: 'EXECUTING',
    review_pending: 'REVIEWING', review_fix: 'NEEDS FIX',
    done: 'DONE', error: 'ERROR',
  }
</script>

<div class="status-bar">
  <span class="pill pill-{store.session.status}">
    {STATUS_LABELS[store.session.status] ?? store.session.status}
  </span>
  {#if store.session.status !== 'idle'}
    <span class="round-info">R{store.session.round}/{store.session.max_rounds}</span>
  {/if}
</div>
