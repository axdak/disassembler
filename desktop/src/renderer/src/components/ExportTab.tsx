// ExportTab — 导出中心
//
// Source: process JSON or final JSON. Output kinds: single Markdown / Obsidian
// Vault / Dataview / Canvas. Open export dir and force re-export. JSON stays
// authoritative; the Markdown/Vault/Dataview/Canvas outputs are read-only
// mirrors — no write-back.
import { useState } from 'react'
import type { ArtifactStatusResponse } from '../types'
import { TaskStatus } from './TaskStatus'

const KINDS = [
  { key: 'single', label: '单一故事结构大 Markdown' },
  { key: 'vault', label: 'Obsidian Vault' },
  { key: 'dataview', label: 'Dataview 查询笔记' },
  { key: 'canvas', label: 'Canvas 故事图谱' },
] as const

export function ExportTab({
  artifactStatus,
  onCreateTask,
  onOpenArtifact,
}: {
  artifactStatus: ArtifactStatusResponse | null
  onCreateTask: (type: 'export', params: Record<string, unknown>) => Promise<void>
  onOpenArtifact: (path: string) => void
}) {
  const [source, setSource] = useState('final')
  const [kinds, setKinds] = useState<string[]>([])
  const [force, setForce] = useState(false)

  function toggleKind(key: string) {
    setKinds((prev) => prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key])
  }

  async function submit() {
    if (kinds.length === 0) return
    await onCreateTask('export', { source, kinds, force })
    setForce(false)
  }

  const exports = artifactStatus?.exports ?? {}

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>导出中心</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 6, alignItems: 'center', maxWidth: 640, fontSize: 13 }}>
        <label htmlFor="exp-source">数据源</label>
        <select id="exp-source" aria-label="数据源" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="final">最终 JSON (final)</option>
          <option value="process">过程 JSON (process)</option>
        </select>
      </div>
      <fieldset style={{ marginTop: 8, maxWidth: 640, fontSize: 13 }}>
        <legend>输出类型</legend>
        {KINDS.map((k) => (
          <label key={k.key} style={{ display: 'block', margin: '4px 0' }}>
            <input
              type="checkbox"
              aria-label={`输出_${k.key}`}
              checked={kinds.includes(k.key)}
              onChange={() => toggleKind(k.key)}
            /> {k.label}
          </label>
        ))}
      </fieldset>
      <div style={{ marginTop: 8, fontSize: 13 }}>
        <label>
          <input type="checkbox" aria-label="强制重新导出" checked={force} onChange={(e) => setForce(e.target.checked)} /> 强制重新导出
        </label>
      </div>
      <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
        <button onClick={() => onOpenArtifact('导出')}>打开导出目录</button>
        <button onClick={submit} disabled={kinds.length === 0}>创建导出任务</button>
      </div>

      {Object.keys(exports).length > 0 && (
        <>
          <h4 style={{ marginTop: 16 }}>已有导出</h4>
          <table style={{ fontSize: 13, width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr><th style={{ textAlign: 'left' }}>类型</th><th style={{ textAlign: 'center' }}>状态</th><th>打开</th></tr></thead>
            <tbody>
              {Object.entries(exports).map(([key, c]) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td style={{ textAlign: 'center' }}><TaskStatus status={c.status} error={c.error} /></td>
                  <td>{c.paths[0] && <button onClick={() => onOpenArtifact(c.paths[0])} style={{ fontSize: 12 }}>打开</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
