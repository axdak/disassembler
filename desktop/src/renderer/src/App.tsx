import { useEffect, useState } from 'react'
import type { Project, StageEvent, Status } from './types'

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [events, setEvents] = useState<StageEvent[]>([])
  const [newPath, setNewPath] = useState('')
  const [error, setError] = useState('')

  async function refresh() {
    try {
      setProjects((await window.api.listProjects()) as Project[])
    } catch (e: any) {
      setError(String(e?.message || e))
    }
  }

  useEffect(() => {
    refresh()
    const off = window.api.onEvent((ev) =>
      setEvents((prev) => [...prev.slice(-300), ev as StageEvent])
    )
    return off
  }, [])

  useEffect(() => {
    if (selected == null) {
      setStatus(null)
      return
    }
    let stop = false
    const poll = async () => {
      try {
        const s = (await window.api.projectStatus(selected)) as Status
        if (!stop) setStatus(s)
      } catch {
        /* ignore */
      }
      if (!stop) setTimeout(poll, 1500)
    }
    poll()
    return () => {
      stop = true
    }
  }, [selected])

  async function create() {
    if (!newPath) return
    try {
      await window.api.createProject(newPath)
      setNewPath('')
      refresh()
    } catch (e: any) {
      setError(String(e))
    }
  }

  async function run() {
    if (selected == null) return
    try {
      await window.api.startRun(selected)
    } catch (e: any) {
      setError(String(e))
    }
  }

  async function cancel() {
    if (selected == null) return
    try {
      await window.api.cancelRun(selected)
    } catch (e: any) {
      setError(String(e))
    }
  }

  const visibleEvents = events.filter((e) => e.project_id === selected).slice(-80)

  return (
    <div style={{ display: 'flex', height: '100vh', margin: 0, fontFamily: 'system-ui, sans-serif' }}>
      <aside style={{ width: 320, borderRight: '1px solid #ddd', padding: 12, overflowY: 'auto' }}>
        <h3 style={{ margin: '0 0 8px' }}>拆书项目</h3>
        <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
          <input
            placeholder="项目目录路径..."
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
            style={{ flex: 1, padding: 6 }}
          />
          <button onClick={create}>接管</button>
        </div>
        <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {projects.map((p) => (
            <li
              key={p.id}
              onClick={() => setSelected(p.id)}
              style={{
                cursor: 'pointer',
                padding: 8,
                borderRadius: 4,
                background: selected === p.id ? '#eef' : 'transparent',
              }}
            >
              {p.name}
            </li>
          ))}
        </ul>
      </aside>

      <main style={{ flex: 1, padding: 16, overflowY: 'auto' }}>
        {status ? (
          <>
            <h2 style={{ margin: '0 0 4px' }}>{status.project.name}</h2>
            <div style={{ color: '#666', fontSize: 12, marginBottom: 8 }}>{status.project.path}</div>
            <div style={{ marginBottom: 4 }}>
              阶段: <b>{status.current_stage}</b> | 活动运行: {status.active_run ? status.active_run.status : '无'}
            </div>
            <div style={{ marginBottom: 8 }}>
              已完成: {status.completed.join(', ') || '无'}
            </div>
            <div style={{ margin: '8px 0' }}>
              <button onClick={run} disabled={!!status.active_run}>
                运行全流程
              </button>
              <button onClick={cancel} disabled={!status.active_run} style={{ marginLeft: 8 }}>
                取消
              </button>
              <button onClick={refresh} style={{ marginLeft: 8 }}>
                刷新
              </button>
            </div>
            {status.last_error && (
              <div style={{ color: 'crimson', marginBottom: 8 }}>{status.last_error}</div>
            )}
            <h4>事件流</h4>
            <pre
              style={{
                background: '#f5f5f5',
                padding: 8,
                maxHeight: 320,
                overflowY: 'auto',
                fontSize: 12,
                margin: 0,
              }}
            >
              {visibleEvents
                .map((e) => `[${e.type}] ${e.stage || ''} ${e.detail || ''}`)
                .join('\n')}
            </pre>
          </>
        ) : (
          <p>从左侧选择或接管一个拆书项目。</p>
        )}
        {error && <div style={{ color: 'crimson', marginTop: 12 }}>{error}</div>}
      </main>
    </div>
  )
}
