// @vitest-environment jsdom
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ArtifactDrawer, type ArtifactDrawerState } from './ArtifactDrawer'

function renderDrawer(state: Partial<ArtifactDrawerState>) {
  return render(
    <ArtifactDrawer
      state={{ open: true, kind: 'artifact', path: '', content: '', ...state }}
      onClose={() => {}}
    />,
  )
}

/** jsdom has no native PointerEvent constructor, so provide drag coordinates explicitly. */
function firePointer(target: Element, type: string, pointerId: number, clientX: number) {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperties(event, {
    pointerId: { value: pointerId },
    clientX: { value: clientX },
  })
  fireEvent(target, event)
}

describe('ArtifactDrawer', () => {
  it('renders Markdown artifacts with GFM structure', () => {
    renderDrawer({
      path: '章节处理/第001章_分析.md',
      content: '# 章节分析\n\n| 人物 | 行动 |\n| --- | --- |\n| 主角 | 出发 |',
    })

    expect(screen.getByTestId('markdown-preview')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '章节分析' })).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('keeps Delta JSON and logs as source text', () => {
    const delta = '{\n  "op": "replace"\n}'
    const { rerender } = render(
      <ArtifactDrawer
        state={{ open: true, kind: 'artifact', path: '章节处理/第001章_Delta.json', content: delta }}
        onClose={() => {}}
      />,
    )

    expect(screen.getByTestId('source-preview')).toHaveTextContent('"op": "replace"')

    rerender(
      <ArtifactDrawer
        state={{ open: true, kind: 'log', path: 'worker.md', content: '# 原样日志' }}
        onClose={() => {}}
      />,
    )
    expect(screen.getByTestId('source-preview')).toHaveTextContent('# 原样日志')
  })

  it('resizes from the left edge with pointer and keyboard controls', () => {
    const view = renderDrawer({ path: '章节处理/第001章_分析.md', content: '# 章节分析' })
    const drawerView = within(view.container)
    const drawer = drawerView.getByTestId('artifact-drawer')
    const handle = drawerView.getByRole('separator', { name: '调整预览抽屉宽度' })
    const initialWidth = Number.parseFloat(drawer.style.width)

    firePointer(handle, 'pointerdown', 1, 300)
    firePointer(handle, 'pointermove', 1, 220)
    firePointer(handle, 'pointerup', 1, 220)
    expect(Number.parseFloat(drawer.style.width)).toBe(initialWidth + 80)

    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(Number.parseFloat(drawer.style.width)).toBe(initialWidth + 56)
  })
})
