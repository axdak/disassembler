import { useEffect, useState } from 'react'
import type { Project, StageEvent, Status, Chapter, Artifact, LogEntry, QueueItem } from './types'

const STAGES = [
  'chapter_analysis', 'chapter_delta', 'chapter_structure', 'visual_assets',
  'fullbook_analysis', 'final_pack', 'commit_final_draft', 'finalize', 'export',
]

type Tab = 'overview' | 'chapters' | 'preview' | 'logs'
type View = 'project' | 'settings'

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [events, setEvents] = useState<StageEvent[]>([])
  const [view, setView] = useState<View>('project')
  const [tab, setTab] = useState<Tab>('overview')
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [artPath, setArtPath] = useState('')
  const [artContent, setArtContent] = useState('')
  const [logsList, setLogsList] = useState<LogEntry[]>([])
  const [logContent, setLogContent] = useState('')
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [settings, setSettings] = useState<Record<string, unknown>>({})
  const [err, setErr] = useState('')

  async function refreshProjects() {
    try { setProjects((await window.api.listProjects()) as Project[]) } catch (e: any) { setErr(String(e)) }
  }
  async function refreshQueue() {
    try { setQueue((await window.api.queueList()) as QueueItem[]) } catch { /* ignore */ }
  }
  async function refreshSettings() {
    try { setSettings(await window.api.getSettings()) } catch { /* ignore */ }
  }

  useEffect(() => {
    refreshProjects()
    refreshQueue()
    const off = window.api.onEvent((ev) => {
      setEvents((p) => [...p.slice(-400), ev as StageEvent])
      refreshQueue()
    })
    const qi = setInterval(refreshQueue, 3000)
    return () => { off(); clearInterval(qi) }
  }, [])

  useEffect(() => {
    if (selected == null) { setStatus(null); return }
    let stop = false
    const poll = async () => {
      try { const s = (await window.api.projectStatus(selected)) as Status; if (!stop) setStatus(s) } catch { /* ignore */ }
      if (!stop) setTimeout(poll, 1500)
    }
    poll()
    return () => { stop = true }
  }, [selected])

  useEffect(() => {
    if (selected != null && tab === 'chapters') {
      window.api.chapters(selected).then((c) => setChapters(c as Chapter[])).catch(() => {})
    }
  }, [selected, tab])

  useEffect(() => {
    if (selected != null && tab === 'logs') {
      window.api.logs(selected).then((l) => setLogsList(l as LogEntry[])).catch(() => {})
    }
  }, [selected, tab])

  async function createFromBook() {
    try {
      const f = await window.api.pickBook()
      if (!f) return
      await window.api.createFromBook(f)
      refreshProjects()
    } catch (e: any) { setErr(String(e)) }
  }
  async function takeOver() {
    try {
      const d = await window.api.pickFolder()
      if (!d) return
      await window.api.createProject(d)
      refreshProjects()
    } catch (e: any) { setErr(String(e)) }
  }
  async function run() {
    if (selected == null) return
    try { await window.api.startRun(selected) } catch (e: any) { setErr(String(e)) }
  }
  async function cancel() {
    if (selected == null) return
    try { await window.api.cancelRun(selected) } catch (e: any) { setErr(String(e)) }
  }
  async function queueAdd() {
    if (selected == null) return
    try { await window.api.queueAdd(selected); refreshQueue() } catch (e: any) { setErr(String(e)) }
  }
  async function queueStart() { try { await window.api.queueStart(); refreshQueue() } catch (e: any) { setErr(String(e)) } }
  async function queueStop() { try { await window.api.queueStop(); refreshQueue() } catch (e: any) { setErr(String(e)) } }
  async function queueRemove(pos: number) { try { await window.api.queueRemove(pos); refreshQueue() } catch { /* ignore */ } }
  async function loadArtifact(path: string) {
    if (selected == null) return
    try {
      const a = (await window.api.artifact(selected, path)) as Artifact
      setArtPath(path); setArtContent(a.content)
    } catch (e: any) { setErr(String(e)) }
  }
  async function loadLog(name: string) {
    if (selected == null) return
    try { const r = (await window.api.log(selected, name)) as { content: string }; setLogContent(r.content) } catch (e: any) { setErr(String(e)) }
  }
  async function saveSettings() {
    try { await window.api.updateSettings(settings); setErr('设置已保存') } catch (e: any) { setErr(String(e)) }
  }

  const visibleEvents = events.filter((e) => e.project_id === selected).slice(-120)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', margin: 0, fontFamily: 'system-ui, sans-serif' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 12px', borderBottom: '1px solid #ddd', background: '#fafafa' }}>
        <b>Novel Disassembler</b>
        <button onClick={() => setView('project')}>项目</button>
        <button onClick={() => { setView('settings'); refreshSettings() }}>设置</button>
        <span style={{ flex: 1 }} />
        <span>队列 {queue.length}</span>
        <button onClick={queueAdd} disabled={selected == null}>加入队列</button>
        <button onClick={queueStart}>启动队列</button>
        <button onClick={queueStop}>停止</button>
      </div>
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <aside style={{ width: 260, borderRight: '1px solid #ddd', padding: 8, overflowY: 'auto' }}>
          <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
            <button onClick={createFromBook}>新建项目（选书）</button>
            <button onClick={takeOver}>接管目录</button>
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {projects.map((p) => (
              <li key={p.id} onClick={() => { setSelected(p.id); setView('project'); setTab('overview') }} style={{ cursor: 'pointer', padding: 6, borderRadius: 4, background: selected === p.id ? '#eef' : 'transparent' }}>{p.name}</li>
            ))}
          </ul>
          {queue.length > 0 && (
            <>
              <h4 style={{ margin: '12px 0 4px' }}>队列</h4>
              <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: 13 }}>
                {queue.map((q) => (
                  <li key={q.position} style={{ padding: 4 }}>#{q.position} 项目{q.project_id} <button onClick={() => queueRemove(q.position)}>x</button></li>
                ))}
              </ul>
            </>
          )}
        </aside>

        <main style={{ flex: 1, padding: 12, overflowY: 'auto' }}>
          {view === 'settings' ? (
            <SettingsView settings={settings} setSettings={setSettings} onSave={saveSettings} />
          ) : status ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <h2 style={{ margin: 0 }}>{status.project.name}</h2>
                <button onClick={run} disabled={!!status.active_run}>运行全流程</button>
                <button onClick={cancel} disabled={!status.active_run}>取消</button>
                <span style={{ flex: 1 }} />
                <button onClick={() => setTab('overview')}>概览</button>
                <button onClick={() => setTab('chapters')}>章节</button>
                <button onClick={() => setTab('preview')}>预览</button>
                <button onClick={() => setTab('logs')}>日志</button>
              </div>
              <div style={{ color: '#666', fontSize: 12, marginBottom: 8 }}>{status.project.path}</div>
              {tab === 'overview' && (
                <>
                  <Timeline current={status.current_stage} completed={status.completed} />
                  <div style={{ margin: '8px 0' }}>
                    活动运行: {status.active_run ? status.active_run.status : '无'}
                    {status.last_error && <span style={{ color: 'crimson', marginLeft: 8 }}>{status.last_error}</span>}
                  </div>
                  <h4>事件流</h4>
                  <pre style={{ background: '#f5f5f5', padding: 8, maxHeight: 320, overflow: 'auto', fontSize: 12, margin: 0 }}>
                    {visibleEvents.map((e) => `[${e.type}] ${e.stage || ''} ${e.detail || ''}`).join('\n')}
                  </pre>
                </>
              )}
              {tab === 'chapters' && (
                <>
                  <h4>章节状态</h4>
                  <table style={{ fontSize: 13, width: '100%', borderCollapse: 'collapse' }}>
                    <tbody>
                      <tr><th style={{ textAlign: 'left' }}>seq</th><th style={{ textAlign: 'left' }}>标题</th><th>分析</th><th>delta</th><th></th></tr>
                      {chapters.map((c) => (
                        <tr key={String(c.seq)}>
                          <td>{c.seq}</td><td>{c.title}</td>
                          <td style={{ textAlign: 'center' }}>{c.has_analysis ? '✓' : '·'}</td>
                          <td style={{ textAlign: 'center' }}>{c.has_delta ? '✓' : '·'}</td>
                          <td>
                            {c.has_analysis && <button onClick={() => loadArtifact(`章节处理/${c.filename}`)}>分析</button>}
                            {c.has_delta && <button onClick={() => loadArtifact(`章节处理/${c.filename.replace(/\.md$/, '.json')}`)} style={{ marginLeft: 4 }}>delta</button>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              {tab === 'preview' && (
                <>
                  <h4>产物预览（只读）</h4>
                  <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
                    <input value={artPath} onChange={(e) => setArtPath(e.target.value)} placeholder="相对路径，如 全书分析/..." style={{ flex: 1, padding: 4 }} />
                    <button onClick={() => loadArtifact(artPath)}>加载</button>
                  </div>
                  <pre style={{ background: '#f5f5f5', padding: 8, maxHeight: 420, overflow: 'auto', fontSize: 12 }}>
                    {artContent || '(输入路径加载，或从章节页跳转)'}
                  </pre>
                </>
              )}
              {tab === 'logs' && (
                <>
                  <h4>worker 日志</h4>
                  <div style={{ marginBottom: 8 }}>
                    {logsList.map((l) => (
                      <button key={l.name} onClick={() => loadLog(l.name)} style={{ marginRight: 8, fontSize: 12 }}>{l.name}</button>
                    ))}
                    {logsList.length === 0 && <span style={{ color: '#888' }}>暂无日志</span>}
                  </div>
                  <pre style={{ background: '#f5f5f5', padding: 8, maxHeight: 420, overflow: 'auto', fontSize: 12 }}>{logContent || '(选择日志文件)'}</pre>
                </>
              )}
            </>
          ) : (
            <p>从左侧选择或接管一个拆书项目。</p>
          )}
          {err && <div style={{ color: 'crimson', marginTop: 12 }}>{err}</div>}
        </main>
      </div>
    </div>
  )
}

function Timeline({ current, completed }: { current: string | null; completed: string[] }) {
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', fontSize: 11 }}>
      {STAGES.map((s) => {
        const done = completed.includes(s)
        const cur = current === s
        const bg = done ? '#4caf50' : cur ? '#2196f3' : '#ccc'
        return <span key={s} style={{ background: bg, color: '#fff', padding: '3px 6px', borderRadius: 3 }}>{s}</span>
      })}
    </div>
  )
}

function SettingsView({ settings, setSettings, onSave }: {
  settings: Record<string, unknown>
  setSettings: (s: Record<string, unknown>) => void
  onSave: () => void
}) {
  const prov = (settings.worker_provider as string) || 'agy'
  return (
    <div style={{ maxWidth: 520 }}>
      <h3>设置</h3>
      <label>worker provider: </label>
      <select value={prov} onChange={(e) => setSettings({ ...settings, worker_provider: e.target.value })}>
        <option value="agy">agy</option>
        <option value="codebuddy">codebuddy</option>
      </select>
      <p style={{ fontSize: 12, color: '#888' }}>
        其他参数（worker_timeout_seconds、worker_retries、fullbook_tasks 等）可通过后端 <code>/settings</code> 设置。
      </p>
      <button onClick={onSave}>保存</button>
    </div>
  )
}
