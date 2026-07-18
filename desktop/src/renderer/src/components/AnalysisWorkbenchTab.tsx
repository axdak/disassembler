// AnalysisWorkbenchTab — 分析工作台
//
// One card per analysis dimension (the 13 from spec §3.3) showing state
// (missing/latest/stale/queued/running/failed/cancelled) plus last run range
// / time / artifact entry. The create-task form configures chapter range
// (all/range/discrete), targets, custom question, original-text policy and
// force. Plain React state; no extra deps.
import { useState } from 'react'
import type { ArtifactStatusResponse, WorkbenchTask } from '../types'
import { TaskStatus } from './TaskStatus'

interface Dim { key: string; label: string }
const DIMENSIONS: Dim[] = [
  { key: 'summary', label: '章节梗概' },
  { key: 'characters', label: '人物档案与人物关系' },
  { key: 'plot', label: '情节、冲突与伏笔' },
  { key: 'style', label: '文风、节奏与高光' },
  { key: 'worldview', label: '世界观' },
  { key: 'settings', label: '设定体系' },
  { key: 'plotlines', label: '剧情线与交汇矩阵' },
  { key: 'outline', label: '全书大纲' },
  { key: 'detailed_outline', label: '章节细纲' },
  { key: 'visual_assets', label: '视觉资产全书汇总' },
  { key: 'narrative_structure', label: '全书叙事结构' },
  { key: 'report', label: '拆书总报告' },
  { key: 'custom', label: '自定义分析' },
]

function parseTargets(raw: string): string[] {
  return raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
}

export function AnalysisWorkbenchTab({
  artifactStatus,
  tasks,
  onCreateTask,
  onOpenArtifact,
}: {
  artifactStatus: ArtifactStatusResponse | null
  tasks: WorkbenchTask[]
  onCreateTask: (type: 'analysis', params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
}) {
  const [task, setTask] = useState('summary')
  const [chapters, setChapters] = useState('')
  const [targets, setTargets] = useState('')
  const [question, setQuestion] = useState('')
  const [originalText, setOriginalText] = useState('none')
  const [force, setForce] = useState(false)

  function latestForDim(key: string): WorkbenchTask | undefined {
    return tasks
      .filter((t) => t.task_type === 'analysis' && t.params?.task === key)
      .slice(-1)[0]
  }

  async function submit() {
    await onCreateTask('analysis', {
      task,
      chapters: chapters.trim() || 'all',
      targets: parseTargets(targets),
      question: question.trim(),
      original_text: originalText,
      force,
    })
    setChapters(''); setTargets(''); setQuestion(''); setForce(false)
  }

  const dims = artifactStatus?.dimensions ?? {}

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>分析维度</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 8 }}>
        {DIMENSIONS.map((d) => {
          const card = dims[d.key] ?? { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] }
          const latest = latestForDim(d.key)
          return (
            <div key={d.key} style={{ border: '1px solid #ddd', borderRadius: 4, padding: 8, fontSize: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <b>{d.label}</b>
                <TaskStatus status={card.status} error={card.error} />
              </div>
              <div style={{ color: '#666' }}>
                {latest ? (
                  <>
                    <div>范围: {String(latest.params?.chapters ?? '-')} · 更新: {latest.updated_at?.slice(0, 19) || '-'}</div>
                    {card.paths[0] && (
                      <button onClick={() => onOpenArtifact(card.paths[0])} style={{ fontSize: 12, marginTop: 4 }}>打开产物</button>
                    )}
                  </>
                ) : '尚未运行'}
              </div>
            </div>
          )
        })}
      </div>

      <h4 style={{ marginTop: 16 }}>创建分析任务</h4>
      <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 6, alignItems: 'center', maxWidth: 640, fontSize: 13 }}>
        <label htmlFor="aw-task">维度</label>
        <select id="aw-task" value={task} onChange={(e) => setTask(e.target.value)}>
          {DIMENSIONS.map((d) => <option key={d.key} value={d.key}>{d.label}</option>)}
        </select>
        <label htmlFor="aw-chapters">章节范围</label>
        <input id="aw-chapters" aria-label="章节范围" value={chapters} onChange={(e) => setChapters(e.target.value)} placeholder="all / 1-10 / 1,3,5" style={{ padding: 4 }} />
        <label htmlFor="aw-targets">目标元素</label>
        <input id="aw-targets" aria-label="目标元素" value={targets} onChange={(e) => setTargets(e.target.value)} placeholder="角色:林婉，事件:…" style={{ padding: 4 }} />
        <label htmlFor="aw-question">自定义问题</label>
        <input id="aw-question" aria-label="自定义问题" value={question} onChange={(e) => setQuestion(e.target.value)} style={{ padding: 4 }} />
        <label htmlFor="aw-orig">原文策略</label>
        <select id="aw-orig" aria-label="原文策略" value={originalText} onChange={(e) => setOriginalText(e.target.value)}>
          <option value="none">none</option>
          <option value="summary">summary</option>
          <option value="full">full</option>
        </select>
        <label htmlFor="aw-force">强制重跑</label>
        <input id="aw-force" aria-label="强制重跑" type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
      </div>
      <button onClick={submit} style={{ marginTop: 8 }}>创建分析任务</button>
    </div>
  )
}
