// Pure event handler — translates bridge/server.py handle(e) L965-1121
// Input: event + current state → Output: state mutations
// No DOM, no side effects, fully testable

import type { BridgeEvent, AppState, LogEntry, AgentPanel, DisplayNames } from './types.js'

function pushLog(state: AppState, agent: AgentPanel, entry: LogEntry): void {
  state.logs[agent].push(entry)
}

function pushBothLogs(state: AppState, entry: LogEntry): void {
  state.logs.planner.push(entry)
  state.logs.reviewer.push(entry)
}

export function handleEvent(e: BridgeEvent, state: AppState, names: DisplayNames): void {
  switch (e.type) {
    case 'round_start': {
      const text = `══════ 第 ${e.data.round} / ${e.data.max} 轮 ══════`
      pushBothLogs(state, { kind: 'separator', level: 'sys', text })
      break
    }

    case 'agent_thinking': {
      state.activeTab[e.data.agent] = 'log'
      pushLog(state, e.data.agent, {
        kind: 'separator', level: 'sys',
        text: `[${names[e.data.agent]} 处理中...]`,
      })
      break
    }

    case 'agent_chunk': {
      const ct = e.data.chunk_type ?? 'text'
      const agent = e.data.agent

      if (ct === 'command') {
        pushLog(state, agent, { kind: 'command', text: e.data.text })
      } else if (ct === 'command_output') {
        const currentFold = state.activeFold[agent]
        if (currentFold) {
          pushLog(state, agent, { kind: 'fold_chunk', foldId: currentFold, text: e.data.text })
        } else {
          const foldId = `fold_${++state.foldSeq}`
          state.activeFold[agent] = foldId
          pushLog(state, agent, { kind: 'fold_start', foldId, label: '命令输出', foldType: 'command_output' })
          pushLog(state, agent, { kind: 'fold_chunk', foldId, text: e.data.text })
        }
      } else {
        pushLog(state, agent, { kind: 'text', text: e.data.text })
      }
      break
    }

    case 'chunk_boundary': {
      const agent = e.data.agent
      if (state.activeFold[agent]) {
        pushLog(state, agent, { kind: 'fold_end', foldId: state.activeFold[agent]! })
        state.activeFold[agent] = null
      }
      break
    }

    case 'agent_stderr': {
      if (e.data.is_mcp) {
        pushLog(state, e.data.agent, { kind: 'mcp', text: e.data.text })
      }
      break
    }

    case 'agent_result': {
      // Currently a no-op in the frontend (server.py L1004-1005)
      break
    }

    case 'agent_response': {
      const role = e.data.role
      if (role === 'user') {
        pushBothLogs(state, {
          kind: 'separator', level: 'sys',
          text: `[你] ${e.data.content}`,
        })
        break
      }
      const agent = role as AgentPanel
      pushLog(state, agent, {
        kind: 'separator', level: 'sys',
        text: `── ${e.data.phase} 完成 (R${e.data.round}) ──`,
      })
      if (e.data.content) {
        if (!state.versions[agent].some(v => v.round === e.data.round)) {
          state.versions[agent].push({
            round: e.data.round,
            phase: e.data.phase,
            content: e.data.content,
          })
        }
        state.activeVer[agent] = -1
        state.showExecResult = false
      }
      state.activeTab[agent] = 'result'

      // Cross-panel notification
      const other: AgentPanel = agent === 'planner' ? 'reviewer' : 'planner'
      if (agent === 'planner') {
        pushLog(state, 'reviewer', {
          kind: 'separator', level: 'sys',
          text: `── ${names.planner} R${e.data.round} 方案已发送给 ${names.reviewer} ──`,
        })
        if (e.data.content) {
          pushLog(state, 'reviewer', {
            kind: 'collapsible',
            label: `查看发送给 ${names.reviewer} 的方案内容`,
            content: e.data.content,
            open: false,
          })
        }
      } else {
        pushLog(state, 'planner', {
          kind: 'separator', level: 'sys',
          text: `── ${names.reviewer} R${e.data.round} 审查意见已发送给 ${names.planner} ──`,
        })
        if (e.data.content) {
          pushLog(state, 'planner', {
            kind: 'collapsible',
            label: `查看发送给 ${names.planner} 的审查意见`,
            content: e.data.content,
            open: false,
          })
        }
      }
      break
    }

    case 'consensus_reached': {
      pushBothLogs(state, { kind: 'separator', level: 'ok', text: `✓ ${e.data.msg}` })
      break
    }

    case 'max_rounds_reached': {
      pushBothLogs(state, { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      break
    }

    case 'execution_done': {
      const ep = e.data.executor_panel
      state.executorPanel = ep
      pushLog(state, ep, { kind: 'separator', level: 'ok', text: '══════ 执行完成 ══════' })
      state.executionResult = e.data.result
      state.showExecResult = true
      state.doneBadge = '✓ 执行完毕'
      break
    }

    case 'error': {
      pushBothLogs(state, { kind: 'separator', level: 'err', text: `❌ ${e.data.msg}` })
      break
    }

    case 'warning': {
      pushLog(state, 'planner', { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      break
    }

    case 'rollback': {
      state.versions.planner = state.versions.planner.filter(v => v.round <= e.data.round)
      state.versions.reviewer = state.versions.reviewer.filter(v => v.round <= e.data.round)
      state.activeVer.planner = -1
      state.activeVer.reviewer = -1
      pushBothLogs(state, { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      break
    }

    case 'status_change': {
      if (['paused', 'aborted', 'interrupted'].includes(e.data.status)) {
        pushBothLogs(state, {
          kind: 'separator',
          level: 'sys',
          text: e.data.msg || '⏹ 已中止',
        })
      }
      break
    }

    case 'review_response': {
      const rrole = e.data.role
      pushLog(state, rrole, {
        kind: 'separator', level: 'sys',
        text: `── ${e.data.phase} (审查轮 ${e.data.round}) ──`,
      })
      if (rrole === 'reviewer' && e.data.content) {
        pushLog(state, 'reviewer', {
          kind: 'collapsible',
          label: `${names.reviewer} 审查意见`,
          content: e.data.content, open: true,
        })
        pushLog(state, 'planner', {
          kind: 'collapsible',
          label: `查看 ${names.reviewer} 审查意见`,
          content: e.data.content, open: false,
        })
      } else if (rrole === 'planner' && e.data.content) {
        pushLog(state, 'planner', {
          kind: 'collapsible',
          label: `${names.planner} 修复总结`,
          content: e.data.content, open: true,
        })
        pushLog(state, 'reviewer', {
          kind: 'collapsible',
          label: `查看 ${names.planner} 修复总结`,
          content: e.data.content, open: false,
        })
      }
      break
    }

    case 'review_start': {
      pushBothLogs(state, { kind: 'separator', level: 'ok', text: '══════ 执行后审查开始 ══════' })
      break
    }

    case 'review_round_start': {
      pushBothLogs(state, {
        kind: 'separator', level: 'sys',
        text: `══════ 审查修复轮 ${e.data.round} / ${e.data.max} ══════`,
      })
      break
    }

    case 'review_needs_fix': {
      pushBothLogs(state, { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      break
    }

    case 'review_done': {
      if (e.data.success) {
        pushBothLogs(state, { kind: 'separator', level: 'ok', text: '══════ 任务收口成功 ══════' })
      } else {
        pushBothLogs(state, { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      }
      state.doneBadge = e.data.success ? '✓ 收口成功' : '⚠ 审查完成'
      break
    }

    case 'review_max_rounds_reached': {
      pushBothLogs(state, { kind: 'separator', level: 'sys', text: `⚠ ${e.data.msg}` })
      break
    }

    case 'cli_start': {
      // No UI action in current frontend
      break
    }
  }
}
