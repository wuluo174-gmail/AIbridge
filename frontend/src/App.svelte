<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import {
    ROLE_ORDER,
    ROLE_TITLES,
    artifactFromStreamEvent,
    appendSceneProjection,
    appendTerminalProjection,
    interventionFromStreamEvent,
    laneFromStreamEvent,
    sessionStateFromStreamEvent,
    sessionStateToSummary,
    sessionSummaryFromStreamEvent,
    type Artifact,
    type Intervention,
    type LaneInfo,
    type RoleKey,
    type SceneItem,
    type SessionState,
    type SessionSummary,
    type StreamEvent,
    type TerminalViewport,
    type TerminalProjection,
    type ViewMode,
    upsertArtifact,
    upsertIntervention,
    upsertLaneInfo,
    upsertSessionSummary,
  } from './lib/workspace.js'

  interface ToolInfo {
    id: string
    display_name: string
    agent_name: string
    detected_installed: boolean
    capabilities: Record<string, boolean>
    executable_path: string | null
    version: string | null
    probe_error: string | null
    last_checked_at: string | null
  }

  interface WorkflowRole {
    role_key: RoleKey
    tool_id: string
    enabled: boolean
    sort_order: number
  }

  interface WorkflowConfig {
    view_mode: ViewMode
    workflow_template: string
    max_rounds: number
    max_review_rounds: number
    roles: WorkflowRole[]
  }

  const ROLE_COLORS: Record<RoleKey, string> = {
    planner: 'var(--planner)',
    reviewer: 'var(--reviewer)',
    executor: 'var(--executor)',
    validator: 'var(--validator)',
  }

  let tools: ToolInfo[] = []
  let workflowConfig: WorkflowConfig = {
    view_mode: 'scene',
    workflow_template: 'standard',
    max_rounds: 5,
    max_review_rounds: 3,
    roles: ROLE_ORDER.map((role_key, sort_order) => ({
      role_key,
      tool_id: role_key === 'reviewer' || role_key === 'validator' ? 'codex' : 'claude-code',
      enabled: true,
      sort_order,
    })),
  }
  let session: SessionState = {
    session_id: null,
    task: '',
    project_path: '',
    workflow_template: 'standard',
    view_mode: 'scene',
    status: 'idle',
    active_stage: 'planning',
    current_round: 0,
    current_review_round: 0,
    consensus_round: 0,
    max_rounds: 5,
    max_review_rounds: 3,
    error: null,
    interrupt_reason: null,
    created_at: null,
    updated_at: null,
    finished_at: null,
    resume_available: false,
  }
  let roles: LaneInfo[] = []
  let artifacts: Artifact[] = []
  let interventions: Intervention[] = []
  let streamEvents: StreamEvent[] = []
  let terminalProjection: TerminalProjection = {
    planner: '',
    reviewer: '',
    executor: '',
    validator: '',
  }
  let sceneProjection: SceneItem[] = []
  let sessions: SessionSummary[] = []
  let streamCursor = 0
  let currentSid: string | null = null
  let projectPath = ''
  let task = ''
  let continueReason = ''
  let sceneInput = ''
  let roleInputs: Record<RoleKey, string> = {
    planner: '',
    reviewer: '',
    executor: '',
    validator: '',
  }
  let loading = false
  let info = ''
  let eventSource: EventSource | null = null
  const viewportTimers: Partial<Record<RoleKey, number>> = {}
  const lastViewportKey: Partial<Record<RoleKey, string>> = {}
  let activeTerminalRole: RoleKey = 'planner'
  let measureCanvas: HTMLCanvasElement | null = null
  let workspaceElement: HTMLElement | null = null
  let viewportSyncFrame = 0
  let sortedRoleList: LaneInfo[] = []
  let artifactsByRole: Record<RoleKey, Artifact[]> = {
    planner: [],
    reviewer: [],
    executor: [],
    validator: [],
  }
  let latestArtifacts: Record<RoleKey, Artifact | null> = {
    planner: null,
    reviewer: null,
    executor: null,
    validator: null,
  }

  async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method }
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(body)
    }
    const response = await fetch(path, init)
    const data = await response.json()
    if (!response.ok) {
      throw new Error(data.error || 'Request failed')
    }
    return data as T
  }

  function toolName(toolId: string) {
    return tools.find((tool) => tool.id === toolId)?.display_name || toolId
  }

  function formatTime(ts: string | null) {
    return ts ? new Date(ts).toLocaleString() : ''
  }

  function updateRoleTool(index: number, toolId: string) {
    workflowConfig = {
      ...workflowConfig,
      roles: workflowConfig.roles.map((role, roleIndex) =>
        roleIndex === index ? { ...role, tool_id: toolId } : role,
      ),
    }
  }

  function roleToolTestId(roleKey: RoleKey) {
    return `role-tool-${roleKey}`
  }

  function rolePaneTestId(roleKey: RoleKey) {
    return `role-pane-${roleKey}`
  }

  function roleMetaTestId(roleKey: RoleKey) {
    return `role-meta-${roleKey}`
  }

  function roleMeta(role: LaneInfo) {
    const viewport = role.viewport
    const hasViewport =
      typeof viewport?.cols === 'number' &&
      viewport.cols > 0 &&
      typeof viewport?.rows === 'number' &&
      viewport.rows > 0
    const sizeLabel = hasViewport ? ` · ${viewport.cols}x${viewport.rows}` : ''
    return `${toolName(role.tool_id)} · ${role.lane_status}${sizeLabel}`
  }

  async function refreshToolsAndConfig() {
    const toolResp = await api<{ tools: ToolInfo[] }>('GET', '/api/tools')
    tools = toolResp.tools
    workflowConfig = await api<WorkflowConfig>('GET', '/api/workflow_config')
  }

  async function refreshSessionList() {
    const resp = await api<{ sessions: SessionSummary[] }>('GET', '/api/sessions?limit=50&offset=0')
    sessions = resp.sessions
  }

  function closeStream() {
    eventSource?.close()
    eventSource = null
  }

  function resetViewportSync() {
    for (const roleKey of ROLE_ORDER) {
      const timer = viewportTimers[roleKey]
      if (timer) {
        window.clearTimeout(timer)
        delete viewportTimers[roleKey]
      }
      delete lastViewportKey[roleKey]
    }
  }

  function measureTerminalViewport(node: HTMLElement): TerminalViewport | null {
    const style = getComputedStyle(node)
    const paddingX = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0)
    const paddingY = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0)
    const widthPx = Math.max(0, Math.floor(node.clientWidth - paddingX))
    const heightPx = Math.max(0, Math.floor(node.clientHeight - paddingY))
    if (!widthPx || !heightPx) return null

    measureCanvas ||= document.createElement('canvas')
    const ctx = measureCanvas.getContext('2d')
    if (!ctx) return null

    const fontSize = parseFloat(style.fontSize) || 12
    const fontFamily = style.fontFamily || 'monospace'
    const fontWeight = style.fontWeight || '400'
    ctx.font = `${fontWeight} ${fontSize}px ${fontFamily}`
    const charWidth = Math.max(ctx.measureText('M').width, 1)

    let lineHeight = parseFloat(style.lineHeight)
    if (!Number.isFinite(lineHeight)) {
      lineHeight = fontSize * 1.45
    }

    return {
      width_px: widthPx,
      height_px: heightPx,
      cols: Math.max(1, Math.floor(widthPx / charWidth)),
      rows: Math.max(1, Math.floor(heightPx / lineHeight)),
      updated_at: new Date().toISOString(),
    }
  }

  async function postTerminalResize(roleKey: RoleKey, viewport: TerminalViewport) {
    if (!currentSid || session.view_mode !== 'terminal') return
    try {
      const response = await api<{ ok: boolean; changed?: boolean; lane?: LaneInfo }>('POST', '/api/terminal/resize', {
        session_id: currentSid,
        role_key: roleKey,
        width_px: viewport.width_px,
        height_px: viewport.height_px,
        cols: viewport.cols,
        rows: viewport.rows,
      })
      if (response.lane) {
        const existing = roles.find((item) => item.role_key === response.lane?.role_key)
        roles = upsertLaneInfo(roles, existing ? { ...existing, ...response.lane } : response.lane)
      }
    } catch (error) {
      console.error('terminal resize sync failed', error)
    }
  }

  function queueTerminalResize(roleKey: RoleKey, viewport: TerminalViewport) {
    const fingerprint = [
      currentSid || 'none',
      roleKey,
      viewport.width_px,
      viewport.height_px,
      viewport.cols,
      viewport.rows,
    ].join(':')
    if (lastViewportKey[roleKey] === fingerprint) return
    const timer = viewportTimers[roleKey]
    if (timer) {
      window.clearTimeout(timer)
    }
    viewportTimers[roleKey] = window.setTimeout(() => {
      lastViewportKey[roleKey] = fingerprint
      delete viewportTimers[roleKey]
      void postTerminalResize(roleKey, viewport)
    }, 120)
  }

  function syncVisibleTerminalViewports() {
    if (!currentSid || session.view_mode !== 'terminal' || !workspaceElement) return
    for (const role of sortedRoleList) {
      const surface = workspaceElement.querySelector(
        `[data-testid="${rolePaneTestId(role.role_key)}"] .terminal-view`,
      ) as HTMLElement | null
      const viewport = surface ? measureTerminalViewport(surface) : null
      if (viewport) {
        queueTerminalResize(role.role_key, viewport)
      }
    }
  }

  function scheduleTerminalViewportSync(options: { reset?: boolean } = {}) {
    if (options.reset) {
      resetViewportSync()
    }
    if (viewportSyncFrame) {
      cancelAnimationFrame(viewportSyncFrame)
    }
    viewportSyncFrame = requestAnimationFrame(() => {
      viewportSyncFrame = 0
      void tick().then(() => syncVisibleTerminalViewports())
    })
  }

  function touchCurrentSession(event: StreamEvent) {
    if (!session.session_id) return
    session = { ...session, updated_at: event.ts }
    sessions = upsertSessionSummary(sessions, sessionStateToSummary(session))
  }

  function applyStreamEvent(event: StreamEvent) {
    streamEvents = [...streamEvents, event]
    streamCursor = event.id + 1
    terminalProjection = appendTerminalProjection(terminalProjection, event)
    sceneProjection = appendSceneProjection(sceneProjection, event)

    const nextSession = sessionStateFromStreamEvent(event)
    if (nextSession && nextSession.session_id === currentSid) {
      session = nextSession
      workflowConfig = { ...workflowConfig, view_mode: nextSession.view_mode }
    } else if (currentSid) {
      touchCurrentSession(event)
    }

    const summary = sessionSummaryFromStreamEvent(event)
    if (summary) {
      sessions = upsertSessionSummary(sessions, summary)
    }

    const lane = laneFromStreamEvent(event)
    if (lane) {
      const existing = roles.find((item) => item.role_key === lane.role_key)
      roles = upsertLaneInfo(roles, existing ? { ...existing, ...lane } : lane)
    }

    const artifact = artifactFromStreamEvent(event)
    if (artifact) {
      artifacts = upsertArtifact(artifacts, artifact)
    }

    const intervention = interventionFromStreamEvent(event)
    if (intervention) {
      interventions = upsertIntervention(interventions, intervention)
    }

    if (currentSid && session.view_mode === 'terminal') {
      scheduleTerminalViewportSync()
    }
  }

  function openStream(sid: string, since = 0) {
    closeStream()
    const source = new EventSource(`/api/stream?sid=${sid}&since=${since}`)
    source.addEventListener('open', () => {
      scheduleTerminalViewportSync({ reset: true })
    })
    source.addEventListener('message', (evt) => {
      applyStreamEvent(JSON.parse(evt.data) as StreamEvent)
    })
    eventSource = source
  }

  async function loadSession(sid: string) {
    loading = true
    info = ''
    try {
      resetViewportSync()
      const historyPayload = await api<{
        session: SessionState
        roles: LaneInfo[]
        events: StreamEvent[]
        artifacts: Artifact[]
        interventions: Intervention[]
        projections: {
          terminal: TerminalProjection
          scene: SceneItem[]
        }
        lane_cursors: Record<string, number>
        stream_cursor: number
      }>('GET', `/api/history?sid=${sid}`)
      currentSid = sid
      session = historyPayload.session
      roles = historyPayload.roles
      streamEvents = historyPayload.events
      artifacts = historyPayload.artifacts
      interventions = historyPayload.interventions
      terminalProjection = historyPayload.projections.terminal
      sceneProjection = historyPayload.projections.scene
      streamCursor = historyPayload.stream_cursor
      projectPath = historyPayload.session.project_path
      task = historyPayload.session.task
      sessions = upsertSessionSummary(sessions, sessionStateToSummary(historyPayload.session))
      workflowConfig = await api<WorkflowConfig>('GET', `/api/workflow_config?sid=${sid}`)
      openStream(sid, historyPayload.stream_cursor)
      window.history.replaceState(null, '', `?sid=${sid}`)
    } finally {
      loading = false
    }
  }

  function exitSession() {
    closeStream()
    resetViewportSync()
    currentSid = null
    session = { ...session, session_id: null, status: 'idle', task: '', project_path: '' }
    roles = []
    artifacts = []
    interventions = []
    streamEvents = []
    terminalProjection = { planner: '', reviewer: '', executor: '', validator: '' }
    sceneProjection = []
    streamCursor = 0
    window.history.replaceState(null, '', location.pathname)
  }

  async function startSession() {
    loading = true
    info = ''
    try {
      const response = await api<{ ok: boolean; session_id: string }>('POST', '/api/session/start', {
        project_path: projectPath,
        task,
        max_rounds: workflowConfig.max_rounds,
        max_review_rounds: workflowConfig.max_review_rounds,
        view_mode: workflowConfig.view_mode,
        roles: workflowConfig.roles,
      })
      await refreshSessionList()
      await loadSession(response.session_id)
    } catch (error) {
      info = error instanceof Error ? error.message : String(error)
    } finally {
      loading = false
    }
  }

  async function applyWorkflowConfig() {
    try {
      workflowConfig = await api<WorkflowConfig>('POST', '/api/workflow_config', workflowConfig)
      info = '默认工作流配置已更新。'
    } catch (error) {
      info = error instanceof Error ? error.message : String(error)
    }
  }

  async function action(path: string, payload: Record<string, unknown> = {}) {
    if (!currentSid) return
    try {
      const response = await api<{ ok?: boolean; error?: string }>('POST', path, { session_id: currentSid, ...payload })
      if (response.error) {
        info = response.error
      } else {
        info = ''
      }
    } catch (error) {
      info = error instanceof Error ? error.message : String(error)
    }
  }

  async function switchViewMode(viewMode: ViewMode) {
    if (!currentSid) {
      workflowConfig = { ...workflowConfig, view_mode: viewMode }
      session = { ...session, view_mode: viewMode }
      return
    }
    await action('/api/session/view_mode', { view_mode: viewMode })
    workflowConfig = { ...workflowConfig, view_mode: viewMode }
  }

  async function sendInput(originView: string, roleKey: RoleKey | null, text: string) {
    if (!currentSid || !text.trim()) return
    try {
      const response = await api<{ ok: boolean; error?: string }>('POST', '/api/input', {
        session_id: currentSid,
        origin_view: originView,
        role_key: roleKey,
        text,
      })
      if (response.error) info = response.error
      else info = ''
    } catch (error) {
      info = error instanceof Error ? error.message : String(error)
    }
  }

  async function submitRoleInput(roleKey: RoleKey) {
    const text = roleInputs[roleKey].trim()
    if (!text) return
    await sendInput('terminal', roleKey, text)
    roleInputs = { ...roleInputs, [roleKey]: '' }
  }

  async function submitSceneInput() {
    const text = sceneInput.trim()
    if (!text) return
    await sendInput('scene', null, text)
    sceneInput = ''
  }

  function canExecute() {
    return ['consensus', 'max_rounds'].includes(session.status)
  }

  function canPause() {
    return ['running', 'executing', 'validating', 'repairing'].includes(session.status)
  }

  function canResume() {
    return session.resume_available
  }

  function canFix() {
    return session.status === 'review_fix'
  }

  function canReviewContinue() {
    return session.status === 'review_max_rounds'
  }

  function canContinue() {
    return ['consensus', 'max_rounds'].includes(session.status)
  }

  onMount(() => {
    const handleWindowResize = () => scheduleTerminalViewportSync()
    window.addEventListener('resize', handleWindowResize)
    const fontSet = document.fonts
    const handleFontsReady = () => scheduleTerminalViewportSync()
    fontSet?.addEventListener?.('loadingdone', handleFontsReady)
    void fontSet?.ready.then(() => scheduleTerminalViewportSync()).catch(() => {})

    void (async () => {
      await refreshToolsAndConfig()
      await refreshSessionList()
      const sid = new URLSearchParams(location.search).get('sid')
      if (sid) {
        await loadSession(sid)
      }
    })()

    return () => {
      window.removeEventListener('resize', handleWindowResize)
      fontSet?.removeEventListener?.('loadingdone', handleFontsReady)
    }
  })

  onDestroy(() => {
    if (viewportSyncFrame) {
      cancelAnimationFrame(viewportSyncFrame)
    }
    resetViewportSync()
    closeStream()
  })

  $: sortedRoleList = [...roles].sort((a, b) => a.sort_order - b.sort_order)
  $: artifactsByRole = ROLE_ORDER.reduce(
    (acc, roleKey) => {
      acc[roleKey] = artifacts.filter((artifact) => artifact.role_key === roleKey).slice().reverse()
      return acc
    },
    {
      planner: [],
      reviewer: [],
      executor: [],
      validator: [],
    } as Record<RoleKey, Artifact[]>,
  )
  $: latestArtifacts = ROLE_ORDER.reduce(
    (acc, roleKey) => {
      acc[roleKey] = artifactsByRole[roleKey][0] ?? null
      return acc
    },
    {
      planner: null,
      reviewer: null,
      executor: null,
      validator: null,
    } as Record<RoleKey, Artifact | null>,
  )
</script>

<svelte:head>
  <title>Bridge v4</title>
</svelte:head>

<div class="app-shell">
  <aside class="session-rail">
    <div class="rail-head">
      <div class="eyebrow">Ledger</div>
      <h2>会话账本</h2>
    </div>
    <div class="session-list">
      {#each sessions as item}
        <button class:selected={item.session_id === currentSid} class="session-card" onclick={() => loadSession(item.session_id)}>
          <div class="session-task">{item.task}</div>
          <div class="session-meta">{item.status} · {item.active_stage}</div>
          <div class="session-meta">{formatTime(item.updated_at)}</div>
        </button>
      {/each}
    </div>
  </aside>

  <main class="main-shell">
    <header class="hero">
      <div class="hero-title">Bridge v4</div>
      <div class="status-stack">
        <div class="pill">{session.status}</div>
        <div class="subpill">{session.active_stage}</div>
      </div>
    </header>

    <details class="config-panel" open={!currentSid}>
      <summary class="config-toggle">配置{#if currentSid}<span class="config-hint">{projectPath} · {task.slice(0, 40)}{task.length > 40 ? '…' : ''}</span>{/if}</summary>
      <section class="config-grid">
        <label class="field wide">
          <span>项目路径</span>
          <input data-testid="project-path-input" bind:value={projectPath} placeholder="/path/to/project" />
        </label>
        <label class="field wide">
          <span>任务</span>
          <textarea data-testid="task-input" rows="3" bind:value={task} placeholder="描述本轮工作流要处理的任务"></textarea>
        </label>
        <label class="field">
          <span>显示模式</span>
          <select data-testid="view-mode-select" bind:value={workflowConfig.view_mode}>
            <option value="scene">Scene</option>
            <option value="terminal">Terminal</option>
          </select>
        </label>
        <label class="field">
          <span>协商轮次</span>
          <input type="number" min="1" max="20" bind:value={workflowConfig.max_rounds} />
        </label>
        <label class="field">
          <span>修复轮次</span>
          <input type="number" min="1" max="20" bind:value={workflowConfig.max_review_rounds} />
        </label>
        {#each workflowConfig.roles as role, index}
          <label class="field">
            <span>{ROLE_TITLES[role.role_key]}</span>
            <select
              data-testid={roleToolTestId(role.role_key)}
              value={workflowConfig.roles[index].tool_id}
              onchange={(event) => updateRoleTool(index, (event.currentTarget as HTMLSelectElement).value)}
            >
              {#each tools as tool}
                <option value={tool.id}>{tool.display_name}{tool.detected_installed ? '' : ' (未安装)'}</option>
              {/each}
            </select>
          </label>
        {/each}
        <div class="action-row">
          <button class="ghost" onclick={applyWorkflowConfig}>应用默认配置</button>
          <button data-testid="start-session" class="primary" disabled={loading} onclick={startSession}>启动会话</button>
        </div>
      </section>
    </details>

    {#if currentSid}
      <section class="control-row">
        <button class="ghost" onclick={exitSession}>← 返回</button>
        <span class="bar-spacer"></span>
        {#if canPause()}<button onclick={() => action('/api/session/pause')}>暂停</button>{/if}
        {#if canResume()}<button onclick={() => action('/api/session/resume')}>恢复</button>{/if}
        <button onclick={() => action('/api/session/stop')}>中止</button>
        {#if canExecute()}<button onclick={() => action('/api/session/exec')}>执行</button>{/if}
        {#if canContinue()}<button onclick={() => action('/api/session/continue', { extra_rounds: 3, message: continueReason })}>继续协商</button>{/if}
        {#if canFix()}
          <button onclick={() => action('/api/session/review_fix')}>修复</button>
          <button onclick={() => action('/api/session/review_skip')}>跳过修复</button>
        {/if}
        {#if canReviewContinue()}<button onclick={() => action('/api/session/review_continue', { extra_rounds: 2 })}>继续审查</button>{/if}
        {#if canContinue() || canFix() || canReviewContinue()}
          <input class="reason-input" bind:value={continueReason} placeholder="驳回理由" />
        {/if}
      </section>
    {/if}

    <section class="session-bar">
      <span class="bar-meta" data-testid="current-session-id">{currentSid || '—'}</span>
      <span class="bar-meta">R{session.current_round}/{session.max_rounds}</span>
      <span class="bar-meta" data-testid="current-view-mode">{session.view_mode}</span>
      <span class="bar-meta">{formatTime(session.updated_at)}</span>
      <span class="bar-spacer"></span>
      <button class:active={session.view_mode === 'scene'} class="mode-button" onclick={() => switchViewMode('scene')}>场景</button>
      <button class:active={session.view_mode === 'terminal'} class="mode-button" onclick={() => switchViewMode('terminal')}>终端</button>
    </section>

    {#if info}
      <div class="info-banner">{info}</div>
    {/if}

    <section class="workspace" data-mode={session.view_mode} bind:this={workspaceElement}>
      {#if session.view_mode === 'terminal'}
        <nav class="role-tabs">
          {#each sortedRoleList as role}
            <button
              class="role-tab"
              class:active={activeTerminalRole === role.role_key}
              style={activeTerminalRole === role.role_key ? `border-bottom-color:${ROLE_COLORS[role.role_key]}` : ''}
              onclick={() => { activeTerminalRole = role.role_key }}
            >
              <span class="role-dot" style={`background:${ROLE_COLORS[role.role_key]}`}></span>
              {ROLE_TITLES[role.role_key]}
              <span class="role-tab-meta">{roles.find(r => r.role_key === role.role_key)?.lane_status || 'idle'}</span>
            </button>
          {/each}
        </nav>
        <div class="terminal-single">
          {#each sortedRoleList as role}
            {#if role.role_key === activeTerminalRole}
              <article class="role-pane" data-testid={rolePaneTestId(role.role_key)}>
                <pre class="terminal-view">{terminalProjection[role.role_key] || ''}</pre>
                <div class="role-input">
                  <input
                    bind:value={roleInputs[role.role_key]}
                    placeholder={`给${ROLE_TITLES[role.role_key]}输入文本或 /command`}
                    onkeydown={(event) => event.key === 'Enter' && submitRoleInput(role.role_key)}
                  />
                  <button onclick={() => submitRoleInput(role.role_key)}>发送</button>
                </div>
                {#if artifactsByRole[role.role_key].length > 0}
                  <div class="artifact-panel">
                    {#each artifactsByRole[role.role_key].slice(0, 3) as artifact}
                      <details>
                        <summary>{artifact.artifact_kind} · R{artifact.round}</summary>
                        <pre>{artifact.content}</pre>
                      </details>
                    {/each}
                  </div>
                {/if}
              </article>
            {/if}
          {/each}
        </div>
      {:else}
        <div class="scene-shell">
          <div class="scene-timeline">
            {#each sceneProjection as item}
              <article class={`scene-card ${item.type}`}>
                <div class="scene-card-head">
                  <span>{item.title}</span>
                  <span>{item.meta}</span>
                </div>
                <pre>{item.content}</pre>
              </article>
            {/each}
          </div>
          <div class="scene-composer">
            <input bind:value={sceneInput} placeholder="输入面向当前阶段的约束，或使用 /pause /exec /continue 等命令" onkeydown={(event) => event.key === 'Enter' && submitSceneInput()} />
            <button onclick={submitSceneInput}>发送</button>
          </div>
        </div>
      {/if}
    </section>
  </main>
</div>
