// GovernanceTab — 质量治理
//
// Auto + proactive audit. Range selection: recent unaudited / chapter range /
// whole book. History of corrections, validation reports, before/after/diff,
// affected stale products. Validation passes auto-apply (no manual confirm);
// the backend enforces the rollback path on failure.
import { useState } from 'react'
import type { WorkbenchTask } from '../types'
import { TaskStatus } from './TaskStatus'

type RangeMode = 'recent_unaudited' | 'range' | 'whole'

function chaptersFor(mode: RangeMode, range: string): string {
  if (mode === 'recent_unaudited') return 'recent'
  if (mode === 'whole') return 'all'
  return range.trim() || 'all'
}

export function GovernanceTab({
  tasks,
  pipelineRunning,
  onCreateTask,
  onOpenArtifact,
}: {
  tasks: WorkbenchTask[]
  pipelineRunning: boolean
  onCreateTask: (type: 'governance', params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
}) {
  const [mode, setMode] = useState<RangeMode>('recent_unaudited')
  const [range, setRange] = useState('')
  const [force, setForce] = useState(false)

  const history = tasks.filter((t) => t.task_type === 'governance').slice(-10).reverse()

  async function submit() {
    await onCreateTask('governance', {
      chapters: chaptersFor(mode, range),
      force,
      auto_apply: true,
    })
    setRange(''); setForce(false)
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>质量治理</h3>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', fontSize: 13, maxWidth: 640 }}>
        <label htmlFor="gov-range">审计范围</label>
        <select id="gov-range" aria-label="审计范围" value={mode} onChange={(e) => setMode(e.target.value as RangeMode)}>
          <option value="recent_unaudited">最近未审计</option>
          <option value="range">章节区间</option>
          <option value="whole">全书</option>
        </select>
        {mode === 'range' && (
          <>
            <label htmlFor="gov-chapters">章节区间</label>
            <input id="gov-chapters" aria-label="章节区间" value={range} onChange={(e) => setRange(e.target.value)} placeholder="101-150" style={{ padding: 4, width: 140 }} />
          </>
        )}
        <label>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> 强制重跑
        </label>
        <button onClick={submit}>启动主动审计</button>
      </div>
      <p style={{ fontSize: 12, color: '#888', marginTop: 6 }}>
        校验通过后自动应用治理补丁；失败时回滚过程 JSON，保留任务包、补丁与报告。
      </p>

      <h4 style={{ marginTop: 16 }}>治理历史</h4>
      {history.length === 0 ? (
        <div style={{ color: '#888', fontSize: 13 }}>暂无主动审计记录</div>
      ) : (
        <table style={{ fontSize: 13, width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>#</th>
              <th style={{ textAlign: 'left' }}>范围</th>
              <th style={{ textAlign: 'center' }}>状态</th>
              <th style={{ textAlign: 'left' }}>错误/报告</th>
              <th>产物</th>
            </tr>
          </thead>
          <tbody>
            {history.map((t) => {
              const result = (t.result ?? {}) as { before?: string; after?: string; diff?: string; report?: string; affected_stale?: string[] }
              return (
                <tr key={t.id}>
                  <td>{t.id}</td>
                  <td>{String(t.params?.chapters ?? '-')}</td>
                  <td style={{ textAlign: 'center' }}><TaskStatus status={t.status} error={t.error} /></td>
                  {/* 自动管线持有同项目写入锁时，说明主动审计排队的具体原因。 */}
                  <td style={{ fontSize: 12, color: t.error ? 'crimson' : '#666' }}>
                    {t.error ?? (t.status === 'queued' && pipelineRunning
                      ? '等待当前自动管线结束后执行'
                      : result.report ? '已生成报告' : '-')}
                  </td>
                  <td>
                    {result.before && <button onClick={() => onOpenArtifact(result.before as string)} style={{ fontSize: 12, marginRight: 4 }}>before</button>}
                    {result.after && <button onClick={() => onOpenArtifact(result.after as string)} style={{ fontSize: 12, marginRight: 4 }}>after</button>}
                    {result.diff && <button onClick={() => onOpenArtifact(result.diff as string)} style={{ fontSize: 12, marginRight: 4 }}>diff</button>}
                    {result.report && <button onClick={() => onOpenArtifact(result.report as string)} style={{ fontSize: 12 }}>报告</button>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
