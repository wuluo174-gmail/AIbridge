<script lang="ts">
  import { store } from '../lib/store.svelte.js'
  import type { HistoryResponse, SessionSummary } from '../lib/types.js'

  let { open = $bindable(false) }: { open: boolean } = $props()

  let sessions = $state<SessionSummary[]>([])
  let detail = $state<HistoryResponse | null>(null)
  let selectedSid = $state<string | null>(null)
  let offset = $state(0)
  const limit = 20
  const selectedSession = $derived(sessions.find((session) => session.session_id === selectedSid) ?? null)

  export async function openModal() {
    open = true; offset = 0; detail = null; selectedSid = null
    await loadPage()
  }

  async function loadPage() {
    const r = await store.loadSessionIndex(limit, offset)
    sessions = r.sessions ?? []
    if (sessions.length && !selectedSid) await selectSession(sessions[0].session_id)
  }

  async function selectSession(sid: string) {
    selectedSid = sid
    detail = await store.loadSessionHistory(sid)
  }

  function prevPage() { if (offset >= limit) { offset -= limit; loadPage() } }
  function nextPage() { if (sessions.length >= limit) { offset += limit; loadPage() } }
  function onClose() { open = false }
  function formatTime(ts: string): string { if (!ts) return ''; return new Date(ts).toLocaleString() }
  function roundValue(session: SessionSummary): number { return session.round }

  function roleName(role: string): string {
    if (!selectedSession) return store.t(`role.${role}`)
    let toolId: string | undefined
    if (role === 'planner') toolId = selectedSession.planner_tool_id
    else if (role === 'reviewer') toolId = selectedSession.reviewer_tool_id
    if (toolId) return store.toolMap[toolId]?.display_name ?? store.t(`role.${role}`)
    return role
  }

  async function openSelected(autoResume = false) {
    if (!selectedSession) return
    await store.openSession(selectedSession, autoResume)
    open = false
  }
</script>

{#if open}
  <div class="modal-mask open">
    <div class="modal" style="width:900px;max-height:90vh">
      <div class="modal-hdr">
        <span>{store.t('history.title')}</span>
        <button class="close" onclick={onClose}>&times;</button>
      </div>
      <div class="modal-body" style="padding:0;display:flex;min-height:400px">
        <div class="hist-list" style="width:320px;flex-shrink:0;border-right:1px solid var(--border);overflow-y:auto">
          {#each sessions as s}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="hist-item" class:selected={selectedSid === s.session_id}
              onclick={() => selectSession(s.session_id)}>
              <div class="hist-task">{s.task}</div>
              <div class="hist-meta">
                <span class="pill pill-{s.status}" style="font-size:10px;padding:1px 6px">{store.t('status.' + s.status)}</span>
                R{roundValue(s)}/{s.max_rounds} · {formatTime(s.updated_at || s.created_at)}
              </div>
            </div>
          {/each}
          {#if !sessions.length}
            <div style="padding:20px;text-align:center;color:var(--dim)">{store.t('history.empty')}</div>
          {/if}
          <div style="display:flex;gap:8px;padding:8px 14px;justify-content:center">
            <button class="btn btn-cancel" onclick={prevPage} disabled={offset === 0}>{store.t('history.prev')}</button>
            <button class="btn btn-cancel" onclick={nextPage} disabled={sessions.length < limit}>{store.t('history.next')}</button>
          </div>
        </div>
        <div class="hist-detail" style="flex:1;overflow-y:auto;padding:14px">
          {#if detail && selectedSession}
            <div class="history-toolbar">
              <div class="history-summary">
                <div class="hist-task">{selectedSession.task}</div>
                <div class="hist-meta">
                  <span class="pill pill-{selectedSession.status}" style="font-size:10px;padding:1px 6px">
                    {store.t('status.' + selectedSession.status)}
                  </span>
                  R{roundValue(selectedSession)}/{selectedSession.max_rounds}
                  · {store.t('history.updated')}: {formatTime(selectedSession.updated_at || selectedSession.created_at)}
                  {#if selectedSession.interrupt_reason}
                    · {store.t('history.interrupt_reason')}: {selectedSession.interrupt_reason}
                  {/if}
                </div>
              </div>
              <div class="history-actions">
                <button class="btn btn-cfg" onclick={() => openSelected(false)}>{store.t('common.open')}</button>
                {#if selectedSession.resume_available}
                  <button class="btn btn-cont" onclick={() => openSelected(true)}>{store.t('ctrl.resume')}</button>
                {/if}
              </div>
            </div>

            <div class="hist-section">{store.t('history.negotiation')}</div>
            {#each detail.entries as entry}
              <div style="margin-bottom:12px">
                <span class="ok">── {store.t('history.round_label', { round: entry.round })} {roleName(entry.role)} ({store.tPhase(entry.phase)}) ──</span>
                <pre style="white-space:pre-wrap;word-wrap:break-word;margin:4px 0;font-size:12px">{entry.content}</pre>
              </div>
            {/each}
            <div class="hist-section">{store.t('history.exec_result')}</div>
            {#if detail.execution_result}
              <pre style="white-space:pre-wrap;word-wrap:break-word;font-size:12px">{detail.execution_result}</pre>
            {:else}
              <div style="color:var(--dim)">{store.t('history.no_exec')}</div>
            {/if}
            {#if detail.review_entries.length}
              <div class="hist-section">{store.t('history.review_record')}</div>
              {#each detail.review_entries as entry}
                <div style="margin-bottom:12px">
                  <span class="ok">── {store.t('history.round_label', { round: entry.round })} {roleName(entry.role)} ({store.tPhase(entry.phase)}) ──</span>
                  <pre style="white-space:pre-wrap;word-wrap:break-word;margin:4px 0;font-size:12px">{entry.content}</pre>
                </div>
              {/each}
            {/if}
          {:else}
            <div style="padding:40px;text-align:center;color:var(--dim)">{store.t('history.select_hint')}</div>
          {/if}
        </div>
      </div>
    </div>
  </div>
{/if}
