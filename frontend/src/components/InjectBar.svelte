<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') store.doInject()
  }
</script>

<div class="inject">
  <input type="text" bind:value={store.injectValue} placeholder={store.t('inject.ph')}
    onkeydown={onKeydown} />
  <button class="btn btn-inj" disabled={!store.canInject} onclick={() => store.doInject()}>{store.t('inject.send')}</button>
</div>
