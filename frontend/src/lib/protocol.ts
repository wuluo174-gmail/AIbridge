// Bridge frontend protocol helpers — mirrors bridge/protocol.py

export function isClosureText(text: string): boolean {
  if (!text) return false
  const firstLine = text.trim().split('\n')[0]
  return /^\s*任务收口成功(?:\s|[，。：:,.!！]|$)/.test(firstLine)
}

export const PHASE_KEYS: Record<string, string> = {
  '方案': 'phase.plan',
  '审查': 'phase.review',
  '执行审查': 'phase.exec_review',
  '修复': 'phase.fix',
  '人工干预': 'phase.user_inject',
}
