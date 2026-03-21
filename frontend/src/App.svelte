<script lang="ts">
  import { onMount } from 'svelte'
  import { store } from './lib/store.svelte.js'
  import TabBar from './components/TabBar.svelte'
  import ControlBar from './components/ControlBar.svelte'
  import AgentPanel from './components/AgentPanel.svelte'
  import InjectBar from './components/InjectBar.svelte'
  import PromptModal from './components/PromptModal.svelte'
  import BrowseModal from './components/BrowseModal.svelte'
  import HistoryModal from './components/HistoryModal.svelte'
  import ConfirmModal from './components/ConfirmModal.svelte'

  let promptOpen = $state(false)
  let browseOpen = $state(false)
  let historyOpen = $state(false)
  let browseModal: BrowseModal | undefined = $state()
  let promptModal: PromptModal | undefined = $state()
  let historyModal: HistoryModal | undefined = $state()

  onMount(async () => {
    document.documentElement.setAttribute('data-theme',
      localStorage.getItem('bridge-theme') || 'dark')
    const { urlProject } = await store.initFromUrl()
    if (urlProject) store.projectPath = urlProject
  })

  function openBrowse() {
    browseModal?.openAt(store.projectPath)
  }

  async function openPrompts() {
    promptModal?.open_modal()
  }

  function openHistory() {
    historyModal?.openModal()
  }

  function onBrowseSelect(path: string) {
    store.projectPath = path
  }
</script>

<TabBar />
<ControlBar onBrowse={openBrowse} onOpenPrompts={openPrompts} onOpenHistory={openHistory} />

<div class="panels">
  <AgentPanel agent="planner" />
  <AgentPanel agent="reviewer" />
</div>

<InjectBar />

<PromptModal bind:open={promptOpen} bind:this={promptModal} />
<BrowseModal bind:open={browseOpen} bind:this={browseModal} onSelect={onBrowseSelect} />
<HistoryModal bind:open={historyOpen} bind:this={historyModal} />
<ConfirmModal />
