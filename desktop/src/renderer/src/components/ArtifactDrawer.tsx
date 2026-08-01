// ArtifactDrawer — read-only preview panel moved out of the old `preview`
// tab. Opens an artifact path or worker log name in a side drawer; the
// content is fetched via the App-level `window.api.artifact` / `window.api.log`
// handlers and passed in as `content`. Markdown/Obsidian exports are a
// read-only mirror — there is no write-back affordance here.
import type { CSSProperties } from 'react'

export interface ArtifactDrawerState {
  open: boolean
  kind: 'artifact' | 'log'
  path: string
  content: string
}

export function ArtifactDrawer({
  state,
  onClose,
}: {
  state: ArtifactDrawerState
  onClose: () => void
}) {
  if (!state.open) return null
  const panel: CSSProperties = {
    position: 'fixed', top: 0, right: 0, bottom: 0, width: 540,
    background: '#fff', borderLeft: '1px solid #ddd',
    boxShadow: '-4px 0 12px rgba(0,0,0,0.08)', display: 'flex',
    flexDirection: 'column', zIndex: 30,
  }
  return (
    <aside style={panel} data-testid="artifact-drawer">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderBottom: '1px solid #eee' }}>
        <b>{state.kind === 'log' ? '日志' : '产物预览'}</b>
        <code style={{ flex: 1, fontSize: 12, color: '#555', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{state.path}</code>
        {state.kind === 'artifact' && state.path && (
          <button onClick={() => window.api.openInFolder(state.path)} title="在文件管理器中打开">📂 打开文件夹</button>
        )}
        <button onClick={onClose}>关闭</button>
      </div>
      <pre style={{ margin: 0, padding: 12, flex: 1, overflow: 'auto', background: '#f5f5f5', fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {state.content || '(空)'}
      </pre>
    </aside>
  )
}
