// OverviewTab — 概览
//
// Keeps the automatic-pipeline controls (run / cancel / resume / current stage
// / completed stages / event stream) and adds the independent-workbench
// revision + task + stale summaries and the new-chapter check entry (append
// preview / conflict / apply). The append flow is local to this tab; the
// backend `chaptersAppendPreview` / `chaptersApply` calls surface concise
// Chinese errors with a link into the logs tab.
import { useState } from 'react'
import type { StageEvent, Status, WorkbenchTask, ArtifactStatusResponse, AppendPlan } from '../types'
import { TaskStatus } from './TaskStatus'

const STAGES = [
  'chapter_analysis', 'chapter_delta', 'chapter_structure', 'visual_assets',
  'fullbook_analysis', 'final_pack', 'commit_final_draft', 'finalize', 'export',
]

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

export function OverviewTab({
  projectId,
  status,
  events,
  tasks,
  artifactStatus,
  onRun,
  onCancel,
  onDelete,
  onOpenLogs,
}: {
  projectId: number
  status: Status
  events: StageEvent[]
  tasks: WorkbenchTask[]
  artifactStatus: ArtifactStatusResponse | null
  onRun: () => void
  onCancel: () => void
  onDelete: () => void
  onOpenLogs: () => void
}) {
  const [sourceFile, setSourceFile] = useState('')
  const [preview, setPreview] = useState<AppendPlan | null>(null)
  const [appendErr, setAppendErr] = useState('')

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

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }}>
        <button onClick={onRun} disabled={!!status.active_run}>运行全流程</button>
        <button onClick={onCancel} disabled={!status.active_run}>取消</button>
        <button onClick={onDelete} style={{ color: 'crimson' }}>删除</button>
      </div>
      <Timeline current={status.current_stage} completed={status.completed} />
      <div style={{ margin: '8px 0' }}>
        活动运行: {status.active_run ? status.active_run.status : '无'}
        {status.last_error && <span style={{ color: 'crimson', marginLeft: 8 }}>{status.last_error}</span>}
      </div>

      <h4 style={{ margin: '12px 0 4px' }}>版本与任务摘要</h4>
      <div style={{ fontSize: 13, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <span>章节索引版本: <code>{revisions.chapter_index || '-'}</code></span>
        <span>故事结构版本: <code>{revisions.story_structure || '-'}</code></span>
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
      <pre style={{ background: '#f5f5f5', padding: 8, maxHeight: 320, overflow: 'auto', fontSize: 12, margin: 0 }}>
        {events.slice(-120).map((e) => `[${e.type}] ${e.stage || ''} ${e.detail || ''}`).join('\n')}
      </pre>
    </div>
  )
}
