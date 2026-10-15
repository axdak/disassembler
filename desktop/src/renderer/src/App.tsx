import { useEffect, useState } from 'react'
import { ModelLimitsSettings, modelLimitsState } from './components/ModelLimitsSettings'
import { ConcurrencySettings, concurrencyError } from './components/ConcurrencySettings'
import type { ConcurrencyPolicy } from './types'
import type { Project, StageEvent, Status, Chapter, LogEntry, QueueItem, TaskType, RecoverSnapshot } from './types'
import { eventRefresh } from './hooks/eventRefresh'
import { useProjectTasks } from './hooks/useProjectTasks'
import { OverviewTab } from './components/OverviewTab'
import { ChaptersTab } from './components/ChaptersTab'
import { AnalysisWorkbenchTab } from './components/AnalysisWorkbenchTab'
import { GovernanceTab } from './components/GovernanceTab'
import { ExportTab } from './components/ExportTab'
import { LogsTab } from './components/LogsTab'
import { MaintenanceTab } from './components/MaintenanceTab'
import { ArtifactDrawer, type ArtifactDrawerState } from './components/ArtifactDrawer'

type Tab = 'overview' | 'chapters' | 'analysis' | 'governance' | 'export' | 'logs' | 'maintenance'
type View = 'project' | 'settings'

const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'chapters', label: '章节' },
  { key: 'analysis', label: '分析工作台' },
  { key: 'governance', label: '质量治理' },
  { key: 'export', label: '导出' },
  { key: 'logs', label: '日志' },
  { key: 'maintenance', label: '维护' },
]

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState<number | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [events, setEvents] = useState<StageEvent[]>([])
  const [view, setView] = useState<View>('project')
  const [tab, setTab] = useState<Tab>('overview')
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [logsList, setLogsList] = useState<LogEntry[]>([])
  const [snapshots, setSnapshots] = useState<RecoverSnapshot[]>([])
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [settings, setSettings] = useState<Record<string, unknown>>({})
  const [concurrencyPolicy, setConcurrencyPolicy] = useState<ConcurrencyPolicy | null>(null)
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  const [drawer, setDrawer] = useState<ArtifactDrawerState>({ open: false, kind: 'artifact', path: '', content: '' })

  const workbench = useProjectTasks(selected, view === 'project' && tab === 'chapters')
  const browsingChapters = view === 'project' && tab === 'chapters'

  async function refreshProjects() {
    try { setProjects((await window.api.listProjects()) as Project[]) } catch (e: any) { setErr(String(e)) }
  }
  async function refreshQueue() {
    try { setQueue((await window.api.queueList()) as QueueItem[]) } catch { /* ignore */ }
  }
  async function refreshSettings() {
    // A missing metadata endpoint must not hide saved settings or imply no cap.
    setConcurrencyPolicy(null)
    await Promise.allSettled([
      window.api.getSettings().then(setSettings),
      window.api.getConcurrencyPolicy().then(setConcurrencyPolicy),
    ])
  }

  useEffect(() => {
    refreshProjects()
    refreshQueue()
    const off = window.api.onEvent((ev) => {
      if ((ev as StageEvent).type === 'connection_restored') { refreshQueue(); return }
      setEvents((p) => [...p.slice(-400), ev as StageEvent])
      if (/^(queue_|pipeline_)/.test((ev as StageEvent).type) || (ev as StageEvent).type === 'run_status') refreshQueue()
    })
    const qi = setInterval(refreshQueue, 3000)
    return () => { off(); clearInterval(qi) }
  }, [])

  useEffect(() => {
    if (selected == null) { setStatus(null); return }
    // Status follows lifecycle events; logs and heartbeats never scan book files.
    let stop = false
    const read = async () => {
      try { const s = (await window.api.projectStatus(selected)) as Status; if (!stop) setStatus(s) } catch { /* ignore */ }
    }
    const refresh = eventRefresh(read)
    if (!browsingChapters) refresh.request()
    const off = window.api.onEvent((ev) => {
      const event = ev as StageEvent
      if (event.type === 'connection_restored' && !browsingChapters) { refresh.request(); return }
      if (event.project_id !== selected || browsingChapters) return
      if (/^(pipeline_|stage_)/.test(event.type) || event.type === 'run_status'
          || event.type === 'chapter_progress'
          || ['task_started', 'task_done', 'task_failed', 'task_cancelled'].includes(event.type)) refresh.request()
    })
    return () => { stop = true; refresh.dispose(); off() }
  }, [selected, browsingChapters])

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

  async function refreshSnapshots() {
    if (selected == null) return
    try { setSnapshots(await window.api.recoverSnapshots(selected)) } catch { /* ignore */ }
  }
  useEffect(() => {
    if (selected != null && tab === 'maintenance') {
      refreshSnapshots()
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
  async function run(endChapterSeq: number) {
    if (selected == null) return
    try { await window.api.startRun(selected, { end_chapter_seq: endChapterSeq }) } catch (e: any) { setErr(String(e)) }
  }
  async function saveRunRange(endChapterSeq: number) {
    if (selected == null) return
    // The returned project is the saved source of truth; keep the current view in sync.
    const project = await window.api.updateProjectRunRange(selected, endChapterSeq)
    setStatus((previous) => previous?.project.id === selected
      ? { ...previous, project } : previous)
  }
  async function cancel() {
    if (selected == null) return
    try { await window.api.cancelRun(selected) } catch (e: any) { setErr(String(e)) }
  }
  async function del() {
    if (selected == null) return
    if (!window.confirm('从项目库移除该项目？（磁盘文件保留，可用「接管目录」加回）')) return
    try { await window.api.deleteProject(selected); setSelected(null); refreshProjects() } catch (e: any) { setErr(String(e)) }
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
      const a = await window.api.artifact(selected, path)
      setDrawer({ open: true, kind: 'artifact', path, content: a.content })
    } catch (e: any) { setErr(String(e)) }
  }
  async function loadLog(name: string) {
    if (selected == null) return
    try {
      const r = await window.api.log(selected, name)
      setDrawer({ open: true, kind: 'log', path: name, content: r.content })
    } catch (e: any) { setErr(String(e)) }
  }
  async function saveSettings() {
    try {
      setSettings(await window.api.updateSettings(settings))
      setErr('')
      setOk('设置已保存')
      setTimeout(() => setOk(''), 3000)
    } catch (e: any) { setOk(''); setErr(String(e)) }
  }

  async function createTask(type: TaskType, params: Record<string, unknown>) {
    await workbench.create(type, params)
  }

  const visibleEvents = events.filter((e) => e.project_id === selected).slice(-120)
  const wbErr = workbench.error

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

        <main style={{ flex: 1, padding: 12, overflowY: 'auto', position: 'relative' }}>
          {view === 'settings' ? (
            <SettingsView settings={settings} setSettings={setSettings} onSave={saveSettings} concurrencyPolicy={concurrencyPolicy} />
          ) : status && selected != null && status.project.id === selected ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                <h2 style={{ margin: 0 }}>{status.project.name}</h2>
                <span style={{ flex: 1 }} />
                {TABS.map((t) => (
                  <button key={t.key} onClick={() => setTab(t.key)} style={{ fontWeight: tab === t.key ? 700 : 400 }}>{t.label}</button>
                ))}
              </div>
              <div style={{ color: '#666', fontSize: 12, marginBottom: 8 }}>{status.project.path}</div>
              {tab === 'overview' && (
                <OverviewTab
                  key={selected}
                  projectId={selected}
                  status={status}
                  events={visibleEvents}
                  tasks={workbench.tasks}
                  artifactStatus={workbench.artifactStatus}
                  onRun={run}
                  onSaveRange={saveRunRange}
                  onCancel={cancel}
                  onDelete={del}
                  onOpenLogs={() => setTab('logs')}
                />
              )}
              {tab === 'chapters' && (
                <ChaptersTab
                  key={selected}
                  chapters={chapters}
                  artifactStatus={workbench.artifactStatus}
                  lastArtifactRefreshAt={workbench.lastArtifactRefreshAt}
                  onRefreshStatus={workbench.refresh}
                  onCreateTask={createTask}
                  onOpenArtifact={loadArtifact}
                  runningTaskId={workbench.tasks.find((t) => t.status === 'running')?.id ?? null}
                  onCancelTask={workbench.cancel}
                />
              )}
              {tab === 'analysis' && (
                <AnalysisWorkbenchTab
                  artifactStatus={workbench.artifactStatus}
                  tasks={workbench.tasks}
                  onCreateTask={createTask}
                  onOpenArtifact={loadArtifact}
                />
              )}
              {tab === 'governance' && (
                <GovernanceTab
                  tasks={workbench.tasks}
                  pipelineRunning={status.active_run?.status === 'running'}
                  onCreateTask={createTask}
                  onOpenArtifact={loadArtifact}
                />
              )}
              {tab === 'export' && (
                <ExportTab
                  projectId={selected}
                  artifactStatus={workbench.artifactStatus}
                  onCreateTask={createTask}
                  onOpenArtifact={loadArtifact}
                />
              )}
              {tab === 'logs' && (
                <LogsTab
                  logsList={logsList}
                  onOpenLog={loadLog}
                />
              )}
              {tab === 'maintenance' && (
                <MaintenanceTab
                  tasks={workbench.tasks}
                  onCreateTask={createTask}
                  onOpenArtifact={loadArtifact}
                  snapshots={snapshots}
                  onRefreshSnapshots={refreshSnapshots}
                />
              )}
            </>
          ) : (
            <p>从左侧选择或接管一个拆书项目。</p>
          )}
          {(err || wbErr) && (
            <div style={{ color: 'crimson', marginTop: 12, fontSize: 13 }}>
              {err || wbErr}
              {wbErr && <button onClick={() => setTab('logs')} style={{ marginLeft: 8 }}>查看日志</button>}
            </div>
          )}
          {ok && (
            <div style={{ color: '#2e7d32', marginTop: 12, fontSize: 13 }}>{ok}</div>
          )}
        </main>
      </div>
      <ArtifactDrawer state={drawer} onClose={() => setDrawer((d) => ({ ...d, open: false }))} />
    </div>
  )
}

export function SettingsView({ settings, setSettings, onSave, concurrencyPolicy }: {
  settings: Record<string, unknown>
  setSettings: (s: Record<string, unknown>) => void
  onSave: () => void
  concurrencyPolicy?: ConcurrencyPolicy | null
}) {
  // 分区样式：浅灰边框 + 顶/底留白，与组件其余行内 style 风格一致
  const sectionStyle = {
    border: '1px solid #e0e0e0', borderRadius: 4, padding: '10px 12px',
    marginTop: 12, background: '#fafafa',
  }
  const groupHintStyle = { fontSize: 12, color: '#888', margin: '4px 0 8px' }
  return (
    <div style={{ maxWidth: 520 }}>
      <h3>设置</h3>
      <ModelLimitsSettings settings={settings} onChange={setSettings} />

      {/* Runtime policy is read-only; editing a task budget never changes the deployment cap. */}
      <ConcurrencySettings settings={settings} onChange={setSettings} policy={concurrencyPolicy} />

      {/* 第二组：分析任务包 —— 全书分析/章节结构分析/视觉资产三类任务共用 build_pack 的打包参数 */}
      <div style={sectionStyle}>
        <h4 style={{ margin: '0 0 6px' }}>分析任务包（字符阈值）</h4>
        <div style={groupHintStyle}>以下参数作用于所有分析任务（全书分析、章节结构分析、视觉资产）的任务包打包。</div>
        <p style={{ fontSize: 12 }}>按分析维度选择章节证据或全书结构资料，历史按容量使用全文或索引。选定的必要证据超限时完整分包，不硬截断；原件保留供查证。</p>
        <div style={{ marginTop: 8 }}>
          <label>单包字符上限: </label>
          <input type="number" min={10000} style={{ width: 80 }}
            value={String((settings.max_pack_chars as number) ?? 70000)}
            onChange={(e) => setSettings({ ...settings, max_pack_chars: Math.max(10000, Number(e.target.value) || 70000) })}
          />
          <span style={{ fontSize: 12, color: '#888' }}> 字符；全书分析任务包超出此值自动分包（多包走 reduce），仅影响阶段5全书分析任务包</span>
        </div>
      </div>

      {/* 第三组：历史表示分级与治理分包；独立于模型 Token 容量 */}
      <div style={sectionStyle}>
        <h4 style={{ margin: '0 0 6px' }}>Delta 与周期治理</h4>
        <div style={groupHintStyle}>历史全文、全局索引和相关档案按预算选择，不逐章重读全部历史。KB 和字符阈值不是模型 Token 上限。</div>
        <div style={{ marginTop: 8 }}>
          <label>Delta 历史全文阈值: </label>
          <input type="number" min={10} style={{ width: 80 }}
            value={String((settings.delta_full_story_kb as number) ?? 256)}
            onChange={(e) => setSettings({ ...settings, delta_full_story_kb: Math.max(10, Number(e.target.value) || 256) })}
          />
          <span style={{ fontSize: 12, color: '#888' }}> KB；达到阈值改用全局名称索引＋最近十章完整 Delta；统一容量不足时可提前切换。完整档案可按需核实。</span>
        </div>
        <div style={{ marginTop: 8 }}>
          <label>周期审计上下文上限: </label>
          <input type="number" min={10000} style={{ width: 80 }}
            value={String((settings.audit_max_context_chars as number) ?? 524288)}
            onChange={(e) => setSettings({ ...settings, audit_max_context_chars: Math.max(10000, Number(e.target.value) || 524288) })}
          />
          <span style={{ fontSize: 12, color: '#888' }}> 字符；先尝试历史全文、索引＋相关详情、索引；仍超限才拆周期或单章证据，原严格校验不变</span>
        </div>
      </div>

      <p style={{ fontSize: 12, color: '#888' }}>
        全书分析任务类型等其他参数可通过后端 <code>/settings</code> 设置。
      </p>
      <button onClick={onSave} disabled={Boolean(modelLimitsState(settings).error || concurrencyError(settings, concurrencyPolicy))}>保存</button>
    </div>
  )
}
