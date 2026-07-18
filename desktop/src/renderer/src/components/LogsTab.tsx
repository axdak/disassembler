// LogsTab — 日志
//
// Worker logs list + content viewer. Clicking a log name opens it in the
// shared artifact drawer (App owns the load + drawer state).
import type { LogEntry } from '../types'

export function LogsTab({
  logsList,
  onOpenLog,
}: {
  logsList: LogEntry[]
  onOpenLog: (name: string) => void
}) {
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>worker 日志</h3>
      <div style={{ marginBottom: 8 }}>
        {logsList.length === 0 ? (
          <span style={{ color: '#888' }}>暂无日志</span>
        ) : (
          logsList.map((l) => (
            <button key={l.name} onClick={() => onOpenLog(l.name)} style={{ marginRight: 8, fontSize: 12 }}>{l.name}</button>
          ))
        )}
      </div>
      <p style={{ fontSize: 12, color: '#888' }}>点击日志名称在右侧抽屉中查看内容。</p>
    </div>
  )
}
