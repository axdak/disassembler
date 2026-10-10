// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Status } from '../types'
import { OverviewTab } from './OverviewTab'

const status = {
  project: { id: 1, name: '测试', path: 'C:/book', created_at: '', updated_at: '' },
  active_run: null,
  current_stage: null,
  completed: [],
  last_error: null,
  recent_events: [],
  revisions: {},
  task_summary: {},
  stale_task_count: 0,
} satisfies Status

describe('截止章节运行入口', () => {
  it('保存输入并在重新打开项目后回填，运行前确认保存', async () => {
    const onRun = vi.fn()
    const onSaveRange = vi.fn(async () => {})
    const props = { projectId: 1, events: [], tasks: [], artifactStatus: null,
      onRun, onSaveRange, onCancel: vi.fn(), onDelete: vi.fn(), onOpenLogs: vi.fn() }
    const view = render(<OverviewTab {...props} status={status} />)

    fireEvent.change(screen.getByLabelText('分析至第'), { target: { value: '3' } })
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('范围已保存'))
    expect(onSaveRange).toHaveBeenLastCalledWith(3)
    view.unmount()

    const reopened = render(<OverviewTab {...props} status={{ ...status, project: { ...status.project, run_end_chapter_seq: 3 } }} />)
    expect((screen.getByLabelText('分析至第') as HTMLInputElement).value).toBe('3')
    fireEvent.click(screen.getByRole('button', { name: '运行全流程（至第 3 章）' }))
    await waitFor(() => expect(onRun).toHaveBeenLastCalledWith(3))
    reopened.unmount()
  })

  it('留空保存全书范围，保存失败时不启动', async () => {
    const onRun = vi.fn()
    const onSaveRange = vi.fn(async () => { throw new Error('磁盘写入失败') })
    const view = render(<OverviewTab projectId={1} status={status} events={[]} tasks={[]}
      artifactStatus={null} onRun={onRun} onSaveRange={onSaveRange} onCancel={vi.fn()}
      onDelete={vi.fn()} onOpenLogs={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '运行全流程（全书）' }))
    await waitFor(() => expect(onSaveRange).toHaveBeenCalledWith(0))
    await waitFor(() => expect(screen.getByText(/保存截止章失败/)).toBeTruthy())
    expect(onRun).not.toHaveBeenCalled()
    view.unmount()
  })

  it('快速输入时按顺序保存，以最后一个有效章节为准', async () => {
    let finishFirst: (() => void) | undefined
    const saved: number[] = []
    const onSaveRange = vi.fn((seq: number) => {
      saved.push(seq)
      return seq === 3 ? new Promise<void>((resolve) => { finishFirst = resolve }) : Promise.resolve()
    })
    const view = render(<OverviewTab projectId={1} status={status} events={[]} tasks={[]}
      artifactStatus={null} onRun={vi.fn()} onSaveRange={onSaveRange} onCancel={vi.fn()}
      onDelete={vi.fn()} onOpenLogs={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('分析至第'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('分析至第'), { target: { value: '31' } })
    await waitFor(() => expect(saved).toEqual([3]))
    finishFirst?.()
    await waitFor(() => expect(saved).toEqual([3, 31]))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('范围已保存'))
    view.unmount()
  })
})
