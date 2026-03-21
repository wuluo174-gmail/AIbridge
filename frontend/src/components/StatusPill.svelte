<script lang="ts">
  import { store } from '../lib/store.svelte.js'
</script>

<div class="status-bar">
  <span class="pill pill-{store.session.status}">
    {store.t('status.' + store.session.status)}
  </span>
  {#if store.session.status !== 'idle'}
    {#if store.session.status.startsWith('review_')}
      <span class="round-info">{store.t('status.review_r')}{store.session.review_round}/{store.session.max_review_rounds}</span>
    {:else}
      <span class="round-info">R{store.session.round}/{store.session.max_rounds}</span>
    {/if}
  {/if}
</div>
