// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChaptersTab } from './ChaptersTab'

/** Large-book regression: scrolling mounts a bounded window without losing selection. */
describe('ChaptersTab virtual list', () => {
  afterEach(cleanup)
  it('renders a moving window and preserves selection across the full scroll range', () => {
    const create = vi.fn(async () => {})
    render(<ChaptersTab
      chapters={Array.from({ length: 3620 }, (_, index) => ({ seq: index + 1, filename: `${index + 1}.md`, title: `章节${index + 1}`, has_analysis: false, has_delta: false }))}
      artifactStatus={null} lastArtifactRefreshAt={null}
      onRefreshStatus={async () => {}} onCreateTask={create}
      onOpenArtifact={() => {}} runningTaskId={null} onCancelTask={async () => {}}
    />)
    expect(screen.getAllByRole('checkbox', { name: /选择章节/ }).length).toBeLessThan(40)
    expect(screen.queryByRole('button', { name: '下一页' })).not.toBeInTheDocument()
    const viewport = screen.getByRole('region', { name: '章节列表' })
    fireEvent.click(screen.getByRole('checkbox', { name: '选择章节1' }))
    fireEvent.scroll(viewport, { target: { scrollTop: 44000 } })
    expect(screen.queryByRole('checkbox', { name: '选择章节1' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('checkbox', { name: /选择章节/ }).length).toBeLessThan(40)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择章节1001' }))
    fireEvent.click(screen.getByRole('button', { name: '重跑 Delta' }))
    expect(create).toHaveBeenLastCalledWith('chapter_delta_rerun', { seqs: [1, 1001] })
    fireEvent.scroll(viewport, { target: { scrollTop: 200000 } })
    expect(screen.getByRole('checkbox', { name: '选择章节3620' })).toBeInTheDocument()
    expect(screen.getAllByRole('checkbox', { name: /选择章节/ }).length).toBeLessThan(40)
    fireEvent.scroll(viewport, { target: { scrollTop: 0 } })
    expect(screen.getByRole('checkbox', { name: '选择章节1' })).toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: '全选章节' }))
    fireEvent.click(screen.getByRole('button', { name: '重跑 Delta' }))
    expect(create).toHaveBeenLastCalledWith('chapter_delta_rerun', { seqs: Array.from({ length: 3620 }, (_, i) => i + 1) })
    fireEvent.click(screen.getByRole('button', { name: '清空选择' }))
    expect(screen.getByRole('button', { name: '重跑 Delta' })).toBeDisabled()
  })

  it('keeps a valid visible range when a refreshed index becomes shorter', () => {
    const props = {
      chapters: Array.from({ length: 100 }, (_, index) => ({ seq: index + 1, filename: `${index + 1}.md`, title: `章节${index + 1}`, has_analysis: false, has_delta: false })),
      artifactStatus: null, lastArtifactRefreshAt: null, runningTaskId: null,
      onRefreshStatus: async () => {}, onCreateTask: async () => {},
      onOpenArtifact: () => {}, onCancelTask: async () => {},
    }
    const { rerender } = render(<ChaptersTab {...props} />)
    const viewport = screen.getByRole('region', { name: '章节列表' })
    fireEvent.scroll(viewport, { target: { scrollTop: 4000 } })
    rerender(<ChaptersTab {...props} chapters={props.chapters.slice(0, 2)} />)
    expect(viewport.scrollTop).toBe(0)
    expect(screen.getAllByRole('checkbox', { name: /选择章节/ })).toHaveLength(2)
  })

  it('prevents repeated manual scans while refresh is pending', async () => {
    let finish!: () => void
    const refresh = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    render(<ChaptersTab chapters={[]} artifactStatus={null} lastArtifactRefreshAt={null}
      onRefreshStatus={refresh} onCreateTask={async () => {}}
      onOpenArtifact={() => {}} runningTaskId={null} onCancelTask={async () => {}}
    />)
    fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
    const pending = screen.getByRole('button', { name: '刷新中…' })
    expect(pending).toBeDisabled()
    fireEvent.click(pending)
    expect(refresh).toHaveBeenCalledTimes(1)
    await act(async () => { finish() })
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新状态' })).toBeEnabled())
  })
})
