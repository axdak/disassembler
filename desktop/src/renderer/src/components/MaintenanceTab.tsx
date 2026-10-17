// MaintenanceTab — 维护
//
// Snapshot recovery (preview list + apply) and direct-submit maintenance
// actions: chronology migration, on-demand finalization, normalize, and delta
// repair, plus a local synopsis-formatting preview. Actions use onCreateTask; the
// backend enforces the writer mutex and the rollback path on failure.
import { useState } from 'react'
import type { WorkbenchTask, RecoverSnapshot, TaskType } from '../types'
import { TaskStatus } from './TaskStatus'

const MAINTENANCE_TYPES: TaskType[] = [
  'recover_story', 'migrate_chronology', 'finalize', 'normalize', 'repair', 'format_synopsis',
]

// Show the new action's user-facing label in queue feedback and task history.
const taskLabel = (type: TaskType) => type === 'format_synopsis' ? '简介排版' : type

export function MaintenanceTab({
  tasks,
  onCreateTask,
  onOpenArtifact,
  snapshots,
  onRefreshSnapshots,
}: {
  tasks: WorkbenchTask[]
  onCreateTask: (type: TaskType, params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
  snapshots: RecoverSnapshot[]
  onRefreshSnapshots: () => void
}) {
  const [chosen, setChosen] = useState<number | null>(null)
  const [msg, setMsg] = useState('')

  const history = tasks
    .filter((t) => MAINTENANCE_TYPES.includes(t.task_type))
    .slice(-10)
    .reverse()

  async function submit(type: TaskType, params: Record<string, unknown> = {}) {
    try {
      await onCreateTask(type, params)
      setMsg(`已排队：${taskLabel(type)}`)
      if (type === 'recover_story') onRefreshSnapshots()
    } catch (e: any) {
      setMsg(String(e))
    }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>维护</h3>

      <section style={{ border: '1px solid #ddd', padding: 12, marginBottom: 12, maxWidth: 640 }}>
        <h4 style={{ marginTop: 0 }}>快照恢复</h4>
        {snapshots.length === 0 ? (
          <div style={{ color: '#888', fontSize: 13 }}>无可用快照</div>
        ) : (
          <select
            aria-label="快照选择"
            value={chosen == null ? '' : String(chosen)}
            onChange={(e) => setChosen(e.target.value === '' ? null : Number(e.target.value))}
            style={{ padding: 4, minWidth: 260 }}
          >
            <option value="">选择快照…</option>
            {snapshots.map((s) => (
              <option key={s.snapshot_seq} value={s.snapshot_seq}>
                seq {s.snapshot_seq}（可重放到 {s.replayable_to}，{s.size} 字节）
              </option>
            ))}
          </select>
        )}
        <div style={{ color: 'crimson', fontSize: 12, marginTop: 6 }}>
          应用将丢弃所选快照之后的所有过程库改动；失败时后端回滚到恢复前状态。
        </div>
        <button
          disabled={chosen == null}
          onClick={() => chosen != null && submit('recover_story', { snapshot_seq: chosen })}
          style={{ marginTop: 6 }}
        >
          应用恢复
        </button>
      </section>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', maxWidth: 640 }}>
        <button onClick={() => submit('migrate_chronology')}>时间线迁移</button>
        <button onClick={() => submit('finalize')}>重新收尾</button>
        <button onClick={() => submit('normalize')}>归一化故事结构</button>
        <button onClick={() => submit('repair', { chapters: 'all' })}>修复 Delta</button>
        <button onClick={() => submit('format_synopsis')}>简介排版</button>
      </div>
      <p style={{ fontSize: 12, color: '#888', marginTop: 6 }}>
        迁移/收尾/归一化/修复均为写者任务，按项目写者互斥串行执行。
      </p>
      <p style={{ fontSize: 12, color: '#888', marginTop: 6 }}>
        简介排版只整理已有标题、段落和空行，生成文本供查看，不改写故事内容；最终收尾会自动整理交付简介。
      </p>

      {msg && <div style={{ marginTop: 12, color: '#444', fontSize: 13 }}>{msg}</div>}

      <h4 style={{ marginTop: 16 }}>维护历史</h4>
      {history.length === 0 ? (
        <div style={{ color: '#888', fontSize: 13 }}>暂无维护任务记录</div>
      ) : (
        <table style={{ fontSize: 13, width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>#</th>
              <th style={{ textAlign: 'left' }}>类型</th>
              <th style={{ textAlign: 'center' }}>状态</th>
              <th style={{ textAlign: 'left' }}>错误/参数</th>
              <th>产物</th>
            </tr>
          </thead>
          <tbody>
            {history.map((t) => {
              const result = (t.result ?? {}) as { artifacts?: string[]; report?: string }
              const artifacts = result.artifacts ?? []
              return (
                <tr key={t.id}>
                  <td>{t.id}</td>
                  <td>{taskLabel(t.task_type)}</td>
                  <td style={{ textAlign: 'center' }}><TaskStatus status={t.status} error={t.error} /></td>
                  <td style={{ fontSize: 12, color: t.error ? 'crimson' : '#666' }}>
                    {t.error ?? (Object.keys(t.params ?? {}).length ? JSON.stringify(t.params) : '-')}
                  </td>
                  <td>
                    {artifacts.map((p) => (
                      <button key={p} onClick={() => onOpenArtifact(p)} style={{ fontSize: 12, marginRight: 4 }}>产物</button>
                    ))}
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
