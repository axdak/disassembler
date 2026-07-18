// TaskStatus — shared badge for workbench task and card states. Renders a
// short Chinese label with a color chip; used by every tab so that state
// vocabulary stays consistent (the backend state strings map 1:1 to labels).
import type { CSSProperties } from 'react'

export type BadgeStatus =
  | 'missing' | 'latest' | 'stale' | 'queued' | 'running' | 'failed' | 'cancelled'
  | 'done' | 'interrupted'

const LABELS: Record<BadgeStatus, string> = {
  missing: '未生成',
  latest: '最新',
  stale: '已过期',
  queued: '排队中',
  running: '运行中',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  done: '完成',
}

const COLORS: Record<BadgeStatus, string> = {
  missing: '#999',
  latest: '#4caf50',
  stale: '#ff9800',
  queued: '#90a4ae',
  running: '#2196f3',
  failed: '#e53935',
  cancelled: '#9e9e9e',
  interrupted: '#7e57c2',
  done: '#4caf50',
}

export function statusLabel(s: string): string {
  return LABELS[s as BadgeStatus] ?? s
}

export function TaskStatus({ status, error }: { status: string; error?: string | null }) {
  const bg = COLORS[status as BadgeStatus] ?? '#999'
  const style: CSSProperties = {
    display: 'inline-block',
    padding: '2px 6px',
    borderRadius: 3,
    background: bg,
    color: '#fff',
    fontSize: 11,
    cursor: 'default',
  }
  return (
    <span style={style} title={error ?? undefined}>
      {statusLabel(status)}
      {error ? ` · ${error}` : ''}
    </span>
  )
}
