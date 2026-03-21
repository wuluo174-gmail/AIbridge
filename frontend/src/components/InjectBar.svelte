<script lang="ts">
  import { store } from '../lib/store.svelte.js'

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') store.doInject()
  }
</script>

<div class="inject">
  <input type="text" bind:value={store.injectValue} placeholder="协商中插入你的意见..."
    onkeydown={onKeydown} />
  <button class="btn btn-inj" disabled={!store.canInject} onclick={() => store.doInject()}>发送</button>
</div>
