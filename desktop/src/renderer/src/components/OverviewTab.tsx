// OverviewTab — 概览
//
// Keeps the automatic-pipeline controls (run / cancel / resume / current stage
// / completed stages / event stream) and adds the independent-workbench
// revision + task + stale summaries and the new-chapter check entry (append
// preview / conflict / apply). The append flow is local to this tab; the
// backend `chaptersAppendPreview` / `chaptersApply` calls surface concise
// Chinese errors with a link into the logs tab.
import { useEffect, useRef, useState } from 'react'
import type { StageEvent, Status, WorkbenchTask, ArtifactStatusResponse, AppendPlan } from '../types'
import { TaskStatus } from './TaskStatus'

const STAGES = [
  'chapter_analysis', 'chapter_delta', 'final_pack', 'commit_final_draft', 'finalize',
  'chapter_structure', 'visual_assets', 'fullbook_analysis', 'export',
]

/** Empty input means the whole book; invalid or fractional chapter numbers stay unsaved. */
function parseEndChapter(value: string): number | null {
  const trimmed = value.trim()
  if (trimmed === '') return 0
  const seq = Number(trimmed)
  return Number.isSafeInteger(seq) && seq > 0 ? seq : null
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

/** Local clock only: lifecycle events supply status without polling book files. */
function RunActivity({ events, running }: { events: StageEvent[]; running: boolean }) {
  const latest = [...events].reverse().find((event) =>
    ['worker_progress', 'recovery_progress', 'chapter_progress'].includes(event.type))
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])
  if (!latest) return null
  const data = latest.data || {}
  const started = Date.parse(latest.ts)
  const elapsed = Number.isFinite(started) ? Math.max(0, (now - started) / 1000) : 0
  const measured = typeof data.elapsed_seconds === 'number' ? data.elapsed_seconds : null
  const delay = typeof data.delay_seconds === 'number' ? data.delay_seconds : null
  return <div aria-label="当前执行详情" style={{ marginTop: 8, padding: 8, background: '#eef5f8', fontSize: 12 }}>
    {running ? '当前执行：' : '最近执行：'}{latest.detail}
    {typeof data.chapter_seq === 'number' && data.chapter_seq > 0 && <> · 第{data.chapter_seq}章</>}
    {measured !== null ? <> · 执行耗时{measured.toFixed(1)}秒</>
      : running && <> · 此步骤已等待{Math.floor(elapsed)}秒</>}
    {running && delay !== null && <> · 预计{Math.max(0, Math.ceil(delay - elapsed))}秒后重试</>}
  </div>
}

// 事件流：按 stage 过滤、type 着色，替代原来一整块 <pre> 纯文本，便于看清阶段与状态。
function EventStream({ events }: { events: StageEvent[] }) {
  const [filter, setFilter] = useState('全部')
  const stages = ['全部', ...Array.from(new Set(events.map((e) => e.stage || '全局')))]
  const visible = filter === '全部' ? events : events.filter((e) => (e.stage || '全局') === filter)
  const colorFor = (type: string) => {
    if (/done$/.test(type)) return '#2e7d32'                 // 完成：绿
    if (/(fail|block|error)/.test(type)) return '#c62828'    // 失败/阻塞：红
    if (/start$/.test(type)) return '#1565c0'                // 开始：蓝
    if (/(progress|handoff)/.test(type)) return '#616161'    // 进度/交接：灰
    return '#333'
  }
  return (
    <>
      <div style={{ marginBottom: 4, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {stages.map((s) => (
          <button key={s} onClick={() => setFilter(s)} style={{ fontSize: 11, fontWeight: filter === s ? 700 : 400, padding: '1px 6px' }}>{s}</button>
        ))}
      </div>
      <div style={{ background: '#f5f5f5', padding: 8, maxHeight: 320, overflow: 'auto', fontSize: 12 }}>
        {visible.length === 0 ? (
          <span style={{ color: '#888' }}>无事件</span>
        ) : (
          visible.map((e, index) => (
            <div key={e.id ?? `${e.ts}-${e.type}-${index}`} style={{ padding: '1px 0' }}>
              <span style={{ color: colorFor(e.type), fontWeight: 600 }}>[{e.type}]</span>{' '}
              <span style={{ color: '#555' }}>{e.stage || ''}</span>{' '}
              <span>{e.detail || ''}</span>
            </div>
          ))
        )}
      </div>
    </>
  )
}

export function OverviewTab({
  projectId,
  status,
  events,
  tasks,
  artifactStatus,
  onRun,
  onSaveRange,
  onCancel,
  onDelete,
  onOpenLogs,
}: {
  projectId: number
  status: Status
  events: StageEvent[]
  tasks: WorkbenchTask[]
  artifactStatus: ArtifactStatusResponse | null
  onRun: (endChapterSeq: number) => Promise<void> | void
  onSaveRange: (endChapterSeq: number) => Promise<void>
  onCancel: () => void
  onDelete: () => void
  onOpenLogs: () => void
}) {
  const [sourceFile, setSourceFile] = useState('')
  const [preview, setPreview] = useState<AppendPlan | null>(null)
  const [appendErr, setAppendErr] = useState('')
  const [endChapterInput, setEndChapterInput] = useState(() =>
    status.project.run_end_chapter_seq ? String(status.project.run_end_chapter_seq) : '')
  const [runErr, setRunErr] = useState('')
  const [rangeSaveState, setRangeSaveState] = useState<'saved' | 'saving' | 'invalid' | 'failed'>('saved')
  const saveVersion = useRef(0)
  const saveTail = useRef<Promise<void>>(Promise.resolve())

  // Serialize writes so rapid typing cannot leave an older chapter number in the database.
  function queueRangeSave(seq: number): Promise<void> {
    const version = ++saveVersion.current
    setRangeSaveState('saving')
    const request = saveTail.current.catch(() => {}).then(() => onSaveRange(seq))
    saveTail.current = request
    void request.then(
      () => { if (saveVersion.current === version) setRangeSaveState('saved') },
      () => { if (saveVersion.current === version) setRangeSaveState('failed') },
    )
    return request
  }

  function changeRunRange(value: string) {
    setEndChapterInput(value)
    setRunErr('')
    const seq = parseEndChapter(value)
    if (seq == null) {
      saveVersion.current++
      setRangeSaveState('invalid')
      return
    }
    void queueRangeSave(seq)
  }

  async function startRun() {
    const seq = parseEndChapter(endChapterInput)
    if (seq == null) {
      setRunErr('截止章请填写正整数，留空表示全书')
      return
    }
    try {
      // Verify persistence before starting, including a retry after an earlier save failure.
      await queueRangeSave(seq)
      setRunErr('')
      await onRun(seq)
    } catch (e) {
      setRunErr(`保存截止章失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  async function checkAppend() {
    if (!sourceFile) { setAppendErr('请先填写源书路径'); return }
    setAppendErr('')
    try {
      const plan = await window.api.chaptersAppendPreview(projectId, { source_file: sourceFile })
      setPreview(plan)
    } catch (e) {
      setAppendErr(`新增章节检查失败：${e instanceof Error ? e.message : String(e)}`)
      setPreview(null)
    }
  }
  async function applyAppend() {
    if (!preview || preview.conflict) return
    try {
      await window.api.chaptersApply(projectId, preview as Record<string, unknown>)
      setPreview(null)
      setSourceFile('')
    } catch (e) {
      setAppendErr(`追加失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const revisions = status.revisions || {}
  const summary = status.task_summary || {}
  const counts = artifactStatus?.counts || {}
  const hasConflict = !!(preview && preview.conflict)
  const appendedList = (preview?.appended as Array<{ seq?: number; filename?: string; title?: string }> | undefined) ?? []
  const conflict = preview?.conflict as { reason?: string; [k: string]: unknown } | null | undefined

  const running = tasks.filter((t) => t.status === 'running')
  const recentFailed = tasks.filter((t) => t.status === 'failed').slice(-3)

  function shortHash(value: unknown): string {
    const s = typeof value === 'string' && value ? value : ''
    return s ? s.slice(0, 8) : '-'
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }}>
        <label htmlFor="end-chapter-seq">分析至第</label>
        <input id="end-chapter-seq" type="number" min={1} step={1} placeholder="全书"
          value={endChapterInput} onChange={(e) => changeRunRange(e.target.value)}
          disabled={!!status.active_run} style={{ width: 72 }} />
        <span>章（含）</span>
        <span role="status" style={{ fontSize: 12, color: rangeSaveState === 'failed' ? 'crimson' : '#666' }}>
          {rangeSaveState === 'saved' ? '范围已保存' : rangeSaveState === 'saving' ? '保存中…'
            : rangeSaveState === 'failed' ? '保存失败，请重试' : '请输入正整数'}
        </span>
        <button onClick={startRun} disabled={!!status.active_run}>
          运行全流程（{parseEndChapter(endChapterInput) == null ? '范围无效'
            : endChapterInput.trim() === '' ? '全书' : `至第 ${endChapterInput.trim()} 章`}）
        </button>
        <button onClick={onCancel} disabled={!status.active_run}>取消</button>
        <button onClick={onDelete} style={{ color: 'crimson' }}>删除</button>
      </div>
      {runErr && <div style={{ color: 'crimson', fontSize: 12 }}>{runErr}</div>}
      <Timeline current={status.current_stage} completed={status.completed} />
      <RunActivity events={events.filter((event) => event.project_id === projectId)} running={status.active_run?.status === 'running'} />
      <div style={{ margin: '8px 0' }}>
        活动运行: {status.active_run ? status.active_run.status : '无'}
        {status.last_error && <span style={{ color: 'crimson', marginLeft: 8 }}>{status.last_error}</span>}
      </div>

      <h4 style={{ margin: '12px 0 4px' }}>版本与任务摘要</h4>
      <div style={{ fontSize: 13, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <span>章节索引版本: <code>{shortHash(revisions.chapter)}</code></span>
        <span>过程结构版本: <code>{shortHash(revisions.process)}</code></span>
        <span>最终结构版本: <code>{shortHash(revisions.final)}</code></span>
        <span>排队: {summary.queued ?? 0}</span>
        <span>运行中: {summary.running ?? 0}</span>
        <span>失败: {summary.failed ?? 0}</span>
        <span>过期任务: {status.stale_task_count ?? 0}</span>
        <span>过期分析: {counts.stale_analysis ?? 0}</span>
        <span>过期导出: {counts.stale_export ?? 0}</span>
      </div>
      {running.length > 0 && (
        <div style={{ fontSize: 13, marginTop: 6 }}>
          运行中任务: {running.map((t) => `#${t.id} ${t.task_type}`).join('，') || '无'}
        </div>
      )}
      {recentFailed.length > 0 && (
        <div style={{ fontSize: 13, marginTop: 6, color: 'crimson' }}>
          最近失败: {recentFailed.map((t) => `#${t.id} ${t.task_type}（${t.error || '未知错误'}）`).join('；')}
          <button onClick={onOpenLogs} style={{ marginLeft: 8 }}>查看日志</button>
        </div>
      )}

      <h4 style={{ margin: '12px 0 4px' }}>新增章节检查</h4>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
        <label style={{ fontSize: 13 }}>源书路径:
          <input
            aria-label="源书路径"
            value={sourceFile}
            onChange={(e) => setSourceFile(e.target.value)}
            placeholder="新版书籍文件路径"
            style={{ width: 320, padding: 4, marginLeft: 6 }}
          />
        </label>
        <button onClick={checkAppend}>检查新增章节</button>
        <button onClick={applyAppend} disabled={!preview || hasConflict}>执行追加</button>
        {appendErr && <span style={{ color: 'crimson', fontSize: 12 }}>{appendErr} <button onClick={onOpenLogs}>查看日志</button></span>}
      </div>
      {hasConflict && (
        <div style={{ background: '#fff3e0', border: '1px solid #ffb74d', padding: 8, fontSize: 13, marginTop: 6 }}>
          <b>冲突，已阻止追加：</b>{conflict?.reason ?? '未知冲突'}
          {Array.isArray(conflict?.mismatch) && (
            <div style={{ marginTop: 4 }}>不一致章节：{String((conflict as { mismatch: unknown[] }).mismatch.join('，'))}</div>
          )}
          <div style={{ marginTop: 4 }}>(标题前缀不一致时不自动覆盖旧章节与旧产物)</div>
        </div>
      )}
      {preview && !hasConflict && appendedList.length > 0 && (
        <div style={{ background: '#e8f5e9', border: '1px solid #81c784', padding: 8, fontSize: 13, marginTop: 6 }}>
          <b>新增章节预览：</b>
          <ul style={{ margin: '4px 0 0', paddingLeft: 20 }}>
            {appendedList.map((c, i) => (
              <li key={i}>第{c.seq ?? '?'}章 {c.title ?? ''} <code style={{ fontSize: 11 }}>{c.filename ?? ''}</code></li>
            ))}
          </ul>
        </div>
      )}

      <h4 style={{ margin: '12px 0 4px' }}>事件流</h4>
      <EventStream events={events.slice(-120)} />
    </div>
  )
}
