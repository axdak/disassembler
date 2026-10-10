// ArtifactDrawer — read-only preview panel moved out of the old `preview`
// tab. Opens an artifact path or worker log name in a side drawer; the
// content is fetched via the App-level `window.api.artifact` / `window.api.log`
// handlers and passed in as `content`. Markdown/Obsidian exports are a
// read-only mirror — there is no write-back affordance here.
import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './ArtifactDrawer.css'

export interface ArtifactDrawerState {
  open: boolean
  kind: 'artifact' | 'log'
  path: string
  content: string
}

const DEFAULT_DRAWER_WIDTH = 720
const MIN_DRAWER_WIDTH = 420
const VIEWPORT_GUTTER = 80
const KEYBOARD_RESIZE_STEP = 24

/** Keep the resizable drawer usable on both compact and wide application windows. */
function drawerWidthLimits() {
  const viewportWidth = typeof window === 'undefined' ? DEFAULT_DRAWER_WIDTH : window.innerWidth
  const max = Math.max(280, viewportWidth - VIEWPORT_GUTTER)
  return { min: Math.min(MIN_DRAWER_WIDTH, max), max }
}

function clampDrawerWidth(width: number): number {
  const { min, max } = drawerWidthLimits()
  return Math.min(max, Math.max(min, width))
}

function initialDrawerWidth(): number {
  const viewportWidth = typeof window === 'undefined' ? DEFAULT_DRAWER_WIDTH : window.innerWidth
  return clampDrawerWidth(Math.min(DEFAULT_DRAWER_WIDTH, viewportWidth * 0.72))
}

/**
 * Only Markdown artifacts use rich rendering. Delta JSON and worker logs stay
 * as source text so punctuation, whitespace and diagnostic output remain exact.
 */
export function isMarkdownArtifact(state: ArtifactDrawerState): boolean {
  return state.kind === 'artifact' && /\.(?:md|markdown)$/i.test(state.path.trim())
}

export function ArtifactDrawer({
  state,
  onClose,
}: {
  state: ArtifactDrawerState
  onClose: () => void
}) {
  const [drawerWidth, setDrawerWidth] = useState(initialDrawerWidth)
  const dragStart = useRef<{ pointerId: number; x: number; width: number } | null>(null)

  useEffect(() => {
    // A window resize can invalidate a previously selected width, so bring it
    // back inside the current viewport without resetting the user's preference.
    const handleWindowResize = () => setDrawerWidth((width) => clampDrawerWidth(width))
    window.addEventListener('resize', handleWindowResize)
    return () => window.removeEventListener('resize', handleWindowResize)
  }, [])

  useEffect(() => {
    const resetDragState = () => {
      dragStart.current = null
      document.body.style.removeProperty('cursor')
      document.body.style.removeProperty('user-select')
    }
    if (!state.open) resetDragState()
    return resetDragState
  }, [state.open])

  if (!state.open) return null
  const renderMarkdown = isMarkdownArtifact(state)
  const widthLimits = drawerWidthLimits()
  const panel: CSSProperties = {
    position: 'fixed', top: 0, right: 0, bottom: 0, width: drawerWidth,
    background: '#fff', borderLeft: '1px solid #ddd',
    boxShadow: '-4px 0 12px rgba(0,0,0,0.08)', display: 'flex',
    flexDirection: 'column', zIndex: 30,
  }

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault()
    dragStart.current = { pointerId: event.pointerId, x: event.clientX, width: drawerWidth }
    event.currentTarget.setPointerCapture?.(event.pointerId)
    document.body.style.cursor = 'ew-resize'
    document.body.style.userSelect = 'none'
  }

  function resizeFromPointer(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current
    if (!start || start.pointerId !== event.pointerId) return
    // The handle is on the left edge: moving left increases drawer width.
    setDrawerWidth(clampDrawerWidth(start.width + start.x - event.clientX))
  }

  function finishResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (dragStart.current?.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    dragStart.current = null
    document.body.style.removeProperty('cursor')
    document.body.style.removeProperty('user-select')
  }

  function resizeFromKeyboard(event: ReactKeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? KEYBOARD_RESIZE_STEP * 2 : KEYBOARD_RESIZE_STEP
    let nextWidth: number | null = null
    if (event.key === 'ArrowLeft') nextWidth = drawerWidth + step
    if (event.key === 'ArrowRight') nextWidth = drawerWidth - step
    if (event.key === 'Home') nextWidth = widthLimits.min
    if (event.key === 'End') nextWidth = widthLimits.max
    if (nextWidth == null) return
    event.preventDefault()
    setDrawerWidth(clampDrawerWidth(nextWidth))
  }

  return (
    <aside style={panel} data-testid="artifact-drawer">
      <div
        className="artifact-drawer-resize-handle"
        role="separator"
        aria-label="调整预览抽屉宽度"
        aria-orientation="vertical"
        aria-valuemin={Math.round(widthLimits.min)}
        aria-valuemax={Math.round(widthLimits.max)}
        aria-valuenow={Math.round(drawerWidth)}
        tabIndex={0}
        title="拖拽调整宽度；方向键可微调"
        onPointerDown={startResize}
        onPointerMove={resizeFromPointer}
        onPointerUp={finishResize}
        onPointerCancel={finishResize}
        onKeyDown={resizeFromKeyboard}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderBottom: '1px solid #eee' }}>
        <b>{state.kind === 'log' ? '日志' : '产物预览'}</b>
        <code style={{ flex: 1, fontSize: 12, color: '#555', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{state.path}</code>
        {state.kind === 'artifact' && state.path && (
          <button onClick={() => window.api.openInFolder(state.path)} title="在文件管理器中打开">📂 打开文件夹</button>
        )}
        <button onClick={onClose}>关闭</button>
      </div>
      {renderMarkdown ? (
        <article className="artifact-markdown" data-testid="markdown-preview">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={{
              // Artifact links open outside the renderer and cannot control the
              // current Electron window through `window.opener`.
              a: ({ children, ...props }) => (
                <a {...props} target="_blank" rel="noreferrer">{children}</a>
              ),
            }}
          >
            {state.content || '(空)'}
          </Markdown>
        </article>
      ) : (
        <pre className="artifact-source" data-testid="source-preview">
          {state.content || '(空)'}
        </pre>
      )}
    </aside>
  )
}
