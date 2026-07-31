// ChaptersTab — 章节工作台
//
// Chapter list with analysis / delta / visual / structure / stale / latest-
// task / error columns. Supports multi-select and the four batch actions:
// rerun chapter analysis, rerun delta, generate per-chapter visual, generate
// per-chapter narrative structure. Preview buttons jump into the artifact
// drawer (read-only).
import { useState } from 'react'
import type { Chapter, ArtifactStatusResponse, ChapterCardStatus, CardStatus, TaskType } from '../types'
import { TaskStatus } from './TaskStatus'

const CARD_KEYS = ['analysis', 'delta', 'visual', 'structure'] as const
const CARD_LABELS: Record<string, string> = { analysis: '分析', delta: 'delta', visual: '视觉', structure: '结构' }
const CARD_HEADERS: Record<string, string> = {
  analysis: '分析态', delta: 'delta态', visual: '视觉态', structure: '结构态',
}

const BATCH_ACTIONS: { label: string; type: TaskType }[] = [
  { label: '重跑章节分析', type: 'chapter_analysis_rerun' },
  { label: '重跑 Delta', type: 'chapter_delta_rerun' },
  { label: '生成分章视觉资产', type: 'chapter_visual' },
  { label: '生成分章叙事结构', type: 'chapter_structure' },
]

function cell(card: CardStatus | undefined): CardStatus {
  return card ?? { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] }
}

export function ChaptersTab({
  chapters,
  artifactStatus,
  onCreateTask,
  onOpenArtifact,
}: {
  chapters: Chapter[]
  artifactStatus: ArtifactStatusResponse | null
  onCreateTask: (type: TaskType, params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set())

  // Prefer artifact-status chapter cards; fall back to the basic chapters list.
  const rows: ChapterCardStatus[] = (artifactStatus?.chapters?.length ?? 0) > 0
    ? (artifactStatus?.chapters ?? [])
    : chapters.map((c) => ({
        seq: (c.seq ?? 0) as number,
        filename: c.filename,
        title: c.title,
        cards: {},
        stale: false,
        latest_task: null,
        error: null,
      }))

  function toggle(seq: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(seq)) next.delete(seq); else next.add(seq)
      return next
    })
  }
  function toggleAll() {
    setSelected((prev) => {
      // 已全选则清空，否则全选
      if (rows.length > 0 && prev.size === rows.length) return new Set()
      return new Set(rows.map((r) => r.seq))
    })
  }
  function runBatch(type: TaskType) {
    const seqs = rows.filter((r) => selected.has(r.seq)).map((r) => r.seq)
    if (seqs.length === 0) return
    // per-chapter adapter（chapter_analysis_rerun/chapter_delta_rerun/chapter_visual/chapter_structure）
    // 均读 params.seqs；勿用 chapters（那是 governance/export 的章节范围字符串参数）。
    onCreateTask(type, { seqs })
  }

  const hasSelection = selected.size > 0
  const allSelected = rows.length > 0 && selected.size === rows.length
  const someSelected = hasSelection && !allSelected

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>章节工作台</h3>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {BATCH_ACTIONS.map((a) => (
          <button key={a.type} onClick={() => runBatch(a.type)} disabled={!hasSelection}>{a.label}</button>
        ))}
        <span style={{ fontSize: 12, color: '#666' }}>已选 {selected.size}/{rows.length}</span>
      </div>
      <table style={{ fontSize: 13, width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={{ textAlign: 'left' }}>
              <input
                type="checkbox"
                aria-label="全选"
                checked={allSelected}
                ref={(el: HTMLInputElement | null) => { if (el) el.indeterminate = someSelected }}
                onChange={toggleAll}
              />
            </th>
            <th style={{ textAlign: 'left' }}>seq</th>
            <th style={{ textAlign: 'left' }}>标题</th>
            {CARD_KEYS.map((k) => <th key={k} style={{ textAlign: 'center' }}>{CARD_HEADERS[k]}</th>)}
            <th style={{ textAlign: 'center' }}>过期</th>
            <th style={{ textAlign: 'left' }}>最近任务/错误</th>
            <th>预览</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.seq}>
              <td>
                <input
                  type="checkbox"
                  aria-label={`选择章节${r.seq}`}
                  checked={selected.has(r.seq)}
                  onChange={() => toggle(r.seq)}
                />
              </td>
              <td>{r.seq}</td>
              <td>{r.title}</td>
              {CARD_KEYS.map((k) => {
                const c = cell(r.cards[k])
                return (
                  <td key={k} style={{ textAlign: 'center' }}>
                    <TaskStatus status={c.status} error={c.error} />
                  </td>
                )
              })}
              <td style={{ textAlign: 'center' }}>{r.stale ? '✓' : '·'}</td>
              <td style={{ fontSize: 12, color: r.error ? 'crimson' : '#666' }}>
                {r.error ?? (r.latest_task ? `#${String(r.latest_task.id ?? '')}` : '·')}
              </td>
              <td>
                {CARD_KEYS.map((k) => {
                  const c = cell(r.cards[k])
                  if (!c.artifact_present || !c.paths[0]) return null
                  return (
                    <button
                      key={k}
                      onClick={() => onOpenArtifact(c.paths[0])}
                      style={{ marginRight: 4, fontSize: 12 }}
                    >{CARD_LABELS[k]}</button>
                  )
                })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
