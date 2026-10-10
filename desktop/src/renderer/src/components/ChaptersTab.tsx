// ChaptersTab — 章节工作台
//
// Chapter list with analysis / delta / visual / structure / stale / latest-
// task / error columns. Supports multi-select and the four batch actions:
// rerun chapter analysis, rerun delta, generate per-chapter visual, generate
// per-chapter narrative structure. Preview buttons jump into the artifact
// drawer (read-only). A fixed-height virtual window bounds DOM size while
// selection covers the full book. Refresh stays outside the scrolling body.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Chapter, ArtifactStatusResponse, ChapterCardStatus, CardStatus, TaskType } from '../types'
import { TaskStatus } from './TaskStatus'
import './ChaptersTab.css'

const CARD_KEYS = ['analysis', 'delta', 'visual', 'structure'] as const
// Keep this height in sync with the table row style; long content is ellipsized.
const ROW_HEIGHT = 44
const OVERSCAN = 8
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
  lastArtifactRefreshAt,
  onRefreshStatus,
  onCreateTask,
  onOpenArtifact,
  runningTaskId,
  onCancelTask,
}: {
  chapters: Chapter[]
  artifactStatus: ArtifactStatusResponse | null
  lastArtifactRefreshAt: number | null
  onRefreshStatus: () => Promise<void>
  onCreateTask: (type: TaskType, params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
  runningTaskId: number | null
  onCancelTask: (taskId: number) => Promise<void>
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [refreshing, setRefreshing] = useState(false)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(600)
  const [headerHeight, setHeaderHeight] = useState(36)
  const viewportRef = useRef<HTMLDivElement>(null)
  const headerRef = useRef<HTMLTableSectionElement>(null)

  // Prefer artifact-status chapter cards; fall back to the basic chapters list.
  const rows = useMemo<ChapterCardStatus[]>(() => (artifactStatus?.chapters?.length ?? 0) > 0
    ? (artifactStatus?.chapters ?? [])
    : chapters.map((c) => ({
        seq: (c.seq ?? 0) as number,
        filename: c.filename,
        title: c.title,
        cards: {},
        stale: false,
        latest_task: null,
        error: null,
      })), [artifactStatus, chapters])

  // Measure the actual scroll viewport, including after window resizes.
  useEffect(() => {
    const viewport = viewportRef.current
    const header = headerRef.current
    if (!viewport || !header) return
    const measure = () => {
      if (viewport.clientHeight > 0) setViewportHeight(viewport.clientHeight)
      if (header.offsetHeight > 0) setHeaderHeight(header.offsetHeight)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(viewport)
    observer.observe(header)
    return () => observer.disconnect()
  }, [])

  const maxScroll = Math.max(0, rows.length * ROW_HEIGHT + headerHeight - viewportHeight)
  const offset = Math.min(scrollTop, maxScroll)
  const firstRow = Math.max(0, Math.floor(offset / ROW_HEIGHT) - OVERSCAN)
  const endRow = Math.min(rows.length, Math.ceil((offset + viewportHeight - headerHeight) / ROW_HEIGHT) + OVERSCAN)
  const visibleRows = rows.slice(firstRow, endRow)
  // A refreshed index may shrink; clamp the viewport rather than showing a blank tail.
  useEffect(() => {
    const viewport = viewportRef.current
    if (viewport && viewport.scrollTop > maxScroll) viewport.scrollTop = maxScroll
    setScrollTop((previous) => Math.min(previous, maxScroll))
  }, [maxScroll])

  async function refreshStatus() {
    if (refreshing) return
    setRefreshing(true)
    try { await onRefreshStatus() } finally { setRefreshing(false) }
  }

  function toggle(seq: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(seq)) next.delete(seq); else next.add(seq)
      return next
    })
  }
  function toggleAll() {
    setSelected((prev) => {
      // 全选针对整本书，不受当前虚拟窗口影响。
      return rows.length > 0 && rows.every((row) => prev.has(row.seq))
        ? new Set() : new Set(rows.map((row) => row.seq))
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
  const allSelected = rows.length > 0 && rows.every((row) => selected.has(row.seq))
  const someSelected = hasSelection && !allSelected

  return (
    <div className="chapters-workbench">
      <h3 style={{ marginTop: 0 }}>章节工作台</h3>
      <div style={{ fontSize: 12, color: '#666', marginBottom: 6 }}>共 {rows.length} 章，连续滚动，已选章节会保留。视觉态的五个分章文件齐全后显示“最新”。</div>
      {/* 工具栏不进入列表滚动区域，刷新按钮始终可见。 */}
      <div className="chapters-toolbar">
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {BATCH_ACTIONS.map((a) => (
            <button key={a.type} onClick={() => runBatch(a.type)} disabled={!hasSelection || runningTaskId != null}>{a.label}</button>
          ))}
          {runningTaskId != null && (
            <button onClick={() => onCancelTask(runningTaskId)} style={{ color: 'crimson' }}>取消运行中任务</button>
          )}
          <span style={{ fontSize: 12, color: '#666' }}>已选 {selected.size}/{rows.length}</span>
          <button onClick={() => setSelected(new Set(rows.map((row) => row.seq)))} disabled={rows.length === 0}>选择全部章节</button>
          <button onClick={() => setSelected(new Set())} disabled={!hasSelection}>清空选择</button>
          <button onClick={() => void refreshStatus()} disabled={refreshing}>{refreshing ? '刷新中…' : '刷新状态'}</button>
          <span role="status" style={{ fontSize: 12, color: '#666' }}>
            状态更新：{lastArtifactRefreshAt == null ? '等待中' : new Date(lastArtifactRefreshAt).toLocaleTimeString('zh-CN', { hour12: false })}（手动刷新）
          </span>
        </div>
      </div>
      <div className="chapters-viewport" ref={viewportRef} role="region" aria-label="章节列表" tabIndex={0}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
        <table className="chapters-table" aria-rowcount={rows.length + 1}>
          <colgroup>
            <col style={{ width: 36 }} /><col style={{ width: 60 }} /><col />
            {CARD_KEYS.map((key) => <col key={key} style={{ width: 78 }} />)}
            <col style={{ width: 48 }} /><col style={{ width: '20%' }} /><col style={{ width: 190 }} />
          </colgroup>
          <thead ref={headerRef}>
            <tr aria-rowindex={1}>
              <th style={{ textAlign: 'left' }}>
                <input
                  type="checkbox"
                  aria-label="全选章节"
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
            {firstRow > 0 && <tr aria-hidden="true"><td colSpan={10} style={{ height: firstRow * ROW_HEIGHT, padding: 0 }} /></tr>}
            {visibleRows.map((r, index) => (
              <tr key={r.seq} className="chapter-row" style={{ height: ROW_HEIGHT }} aria-rowindex={firstRow + index + 2}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`选择章节${r.seq}`}
                    checked={selected.has(r.seq)}
                    onChange={() => toggle(r.seq)}
                  />
                </td>
                <td>{r.seq}</td>
                <td title={r.title}><div className="chapter-cell">{r.title}</div></td>
                {CARD_KEYS.map((k) => {
                  const c = cell(r.cards[k])
                  return (
                    <td key={k} style={{ textAlign: 'center' }}>
                      <div className="chapter-cell"><TaskStatus status={c.status} error={c.error} /></div>
                    </td>
                  )
                })}
                <td style={{ textAlign: 'center' }}>{r.stale ? '✓' : '·'}</td>
                <td title={r.error ?? undefined} style={{ fontSize: 12, color: r.error ? 'crimson' : '#666' }}>
                  <div className="chapter-cell">
                  {r.error ?? (r.latest_task ? `#${String(r.latest_task.id ?? '')}` : '·')}
                  </div>
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
            {endRow < rows.length && <tr aria-hidden="true"><td colSpan={10} style={{ height: (rows.length - endRow) * ROW_HEIGHT, padding: 0 }} /></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
