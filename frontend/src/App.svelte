<script lang="ts">
  import { onMount } from 'svelte'
  import { store } from './lib/store.svelte.js'
  import ControlBar from './components/ControlBar.svelte'
  import AgentPanel from './components/AgentPanel.svelte'
  import InjectBar from './components/InjectBar.svelte'
  import PromptModal from './components/PromptModal.svelte'
  import BrowseModal from './components/BrowseModal.svelte'

  let projectPath = $state('')
  let promptOpen = $state(false)
  let browseOpen = $state(false)
  let browseModal: BrowseModal | undefined = $state()
  let promptModal: PromptModal | undefined = $state()

  onMount(async () => {
    const { urlProject } = await store.initFromUrl()
    if (urlProject) projectPath = urlProject
  })

  function openBrowse() {
    browseModal?.openAt(projectPath)
  }

  async function openPrompts() {
    promptModal?.open_modal()
  }

  function onBrowseSelect(path: string) {
    projectPath = path
  }
</script>

<ControlBar bind:projectPath onBrowse={openBrowse} onOpenPrompts={openPrompts} />

<div class="panels">
  <AgentPanel agent="planner" />
  <AgentPanel agent="reviewer" />
</div>

<InjectBar />

<PromptModal bind:open={promptOpen} bind:this={promptModal} />
<BrowseModal bind:open={browseOpen} bind:this={browseModal} onSelect={onBrowseSelect} />
