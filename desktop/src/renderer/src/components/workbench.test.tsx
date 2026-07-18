// @vitest-environment jsdom
//
// Task 7 renderer interaction tests (TDD). Mounts the App with a fake typed
// `window.api` and drives the six-tab workbench UI: tab switching, analysis
// dimension cards + create form, chapter multi-select + batch actions,
// governance range, export options, append preview/conflict/apply, task-event
// refresh scoped to the selected project, and artifact/log drawer open.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { App } from '../App'
import type {
  Project,
  Status,
  Chapter,
  ArtifactStatusResponse,
  WorkbenchTask,
  AppendPlan,
} from '../types'

// ----- fixtures ----------------------------------------------------------------

const PROJECT: Project = {
  id: 1,
  name: '测试书',
  path: 'C:/proj/测试书',
  created_at: '',
  updated_at: '',
}

const STATUS: Status = {
  project: PROJECT,
  active_run: null,
  current_stage: null,
  completed: ['chapter_analysis'],
  last_error: null,
  recent_events: [],
  revisions: { chapter_index: 'v1', story_structure: 'v1' },
  task_summary: { queued: 1, running: 1, failed: 2 },
  stale_task_count: 3,
}

const CHAPTERS: Chapter[] = [
  { seq: 1, filename: '001.md', title: '第一章 起', has_analysis: true, has_delta: true },
  { seq: 2, filename: '002.md', title: '第二章 承', has_analysis: true, has_delta: false },
]

const ARTIFACT_STATUS: ArtifactStatusResponse = {
  dimensions: {
    summary: { status: 'latest', artifact_present: true, latest_task_id: 10, latest_task_status: 'done', error: null, paths: ['全书分析/章节梗概.md'] },
    plot: { status: 'stale', artifact_present: true, latest_task_id: 11, latest_task_status: 'done', error: null, paths: ['全书分析/情节.md'] },
    custom: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
  },
  chapters: [
    {
      seq: 1, filename: '001.md', title: '第一章 起', stale: false, latest_task: null, error: null,
      cards: {
        analysis: { status: 'latest', artifact_present: true, latest_task_id: 1, latest_task_status: 'done', error: null, paths: ['章节处理/001.md'] },
        delta: { status: 'latest', artifact_present: true, latest_task_id: 2, latest_task_status: 'done', error: null, paths: ['章节处理/001.json'] },
        visual: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
        structure: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
      },
    },
    {
      seq: 2, filename: '002.md', title: '第二章 承', stale: true, latest_task: null, error: '上次失败',
      cards: {
        analysis: { status: 'failed', artifact_present: false, latest_task_id: 3, latest_task_status: 'failed', error: 'worker 超时', paths: [] },
        delta: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
        visual: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
        structure: { status: 'missing', artifact_present: false, latest_task_id: null, latest_task_status: null, error: null, paths: [] },
      },
    },
  ],
  exports: {
    obsidian: { status: 'stale', artifact_present: true, latest_task_id: 20, latest_task_status: 'done', error: null, paths: ['导出/obsidian'] },
  },
  counts: { stale_analysis: 1, stale_export: 1 },
}

const TASKS: WorkbenchTask[] = [
  {
    id: 10, project_id: 1, task_type: 'analysis', status: 'done', params: { task: 'summary' },
    input_revision: {}, result: { paths: ['全书分析/章节梗概.md'] }, error: null,
    cancel_requested: false, retry_of: null, created_at: '', updated_at: '',
    started_at: '', ended_at: '', stale: false,
  },
  {
    id: 30, project_id: 1, task_type: 'governance', status: 'failed', params: { chapters: '1-10' },
    input_revision: {}, result: null, error: '校验失败',
    cancel_requested: false, retry_of: null, created_at: '', updated_at: '',
    started_at: '', ended_at: '', stale: false,
  },
]

// ----- fake window.api --------------------------------------------------------

interface FakeApi {
  api: Window['api']
  calls: {
    tasksList: number
    artifactsStatus: number
    tasksCreate: { type: string; params: Record<string, unknown> }[]
    appendPreview: number
    apply: number
    artifact: number
    log: number
  }
  emit: (ev: unknown) => void
  setAppendPreview: (plan: AppendPlan) => void
}

function mountFakeApi(opts: { appendPreview?: AppendPlan } = {}): FakeApi {
  const handlers: ((ev: unknown) => void)[] = []
  const calls = {
    tasksList: 0, artifactsStatus: 0, tasksCreate: [] as { type: string; params: Record<string, unknown> }[],
    appendPreview: 0, apply: 0, artifact: 0, log: 0,
  }
  let appendPreview: AppendPlan = opts.appendPreview ?? { appended: [{ seq: 3, filename: '003.md', title: '第三章 转' }] }
  const api: Window['api'] = {
    listProjects: async () => [PROJECT],
    createProject: async () => PROJECT,
    projectStatus: async () => STATUS,
    deleteProject: async () => ({ ok: true }),
    startRun: async () => ({ run_id: 1 }),
    cancelRun: async () => ({ cancelled: true }),
    chapters: async () => CHAPTERS,
    artifact: async () => { calls.artifact++; return { path: 'p', size: 0, truncated: false, content: '产物内容预览' } },
    tasksCreate: async (_id, type, params) => { calls.tasksCreate.push({ type, params }); return { ...TASKS[0], type, params } },
    tasksList: async () => { calls.tasksList++; return TASKS },
    taskGet: async () => TASKS[0],
    taskCancel: async () => ({ ...TASKS[0], status: 'cancelled' }),
    taskRetry: async () => ({ ...TASKS[0], status: 'queued' }),
    artifactsStatus: async () => { calls.artifactsStatus++; return ARTIFACT_STATUS },
    chaptersAppendPreview: async () => { calls.appendPreview++; return appendPreview },
    chaptersApply: async () => { calls.apply++; return { applied: true } },
    logs: async () => [{ name: 'worker.log', size: 0, mtime: 0 }],
    log: async () => { calls.log++; return { content: 'worker 日志内容' } },
    getSettings: async () => ({ worker_provider: 'agy' }),
    updateSettings: async () => ({}),
    queueAdd: async () => ({ position: 1 }),
    queueList: async () => [],
    queueRemove: async () => ({ removed: true }),
    queueStart: async () => ({ started: true }),
    queueStop: async () => ({ stopped: true }),
    pickBook: async () => null,
    pickFolder: async () => null,
    createFromBook: async () => PROJECT,
    onEvent: (cb) => { handlers.push(cb); return () => { const i = handlers.indexOf(cb); if (i >= 0) handlers.splice(i, 1) } },
  }
  return {
    api,
    calls,
    emit: (ev) => { act(() => { for (const h of handlers) h(ev) }) },
    setAppendPreview: (p) => { appendPreview = p },
  }
}

function renderApp(fake: FakeApi) {
  const saved = (window as unknown as { api?: Window['api'] }).api
  ;(window as unknown as { api: Window['api'] }).api = fake.api
  const utils = render(<App />)
  return { ...utils, restore: () => { if (saved) (window as unknown as { api: Window['api'] }).api = saved } }
}

async function selectProject() {
  const item = await screen.findByText('测试书')
  fireEvent.click(item)
  // Tabs render once project status loads.
  await screen.findByRole('button', { name: '概览' })
}

function goTab(label: string) {
  fireEvent.click(screen.getByRole('button', { name: label }))
}

// ----- tests ------------------------------------------------------------------

describe('workbench six-tab UI', () => {
  let fake: FakeApi
  let restore: () => void
  let unmount: () => void

  beforeEach(() => {
    fake = mountFakeApi()
    const r = renderApp(fake)
    restore = r.restore
    unmount = r.unmount
  })
  afterEach(() => { unmount(); restore() })

  it('renders the six required tabs after selecting a project', async () => {
    await selectProject()
    for (const label of ['概览', '章节', '分析工作台', '质量治理', '导出', '日志']) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    }
  })

  it('switches between tabs', async () => {
    await selectProject()
    goTab('分析工作台')
    expect(screen.getByText('分析维度')).toBeInTheDocument()
    goTab('导出')
    expect(screen.getByText('导出中心')).toBeInTheDocument()
  })

  it('analysis tab: dimension cards + configurable create form', async () => {
    await selectProject()
    goTab('分析工作台')
    // Dimension cards: at least summary/plot/custom labels. The select option
    // carries the same label, so the dimension card text appears twice.
    expect(screen.getAllByText('章节梗概').length).toBeGreaterThan(0)
    expect(screen.getAllByText('情节、冲突与伏笔').length).toBeGreaterThan(0)
    expect(screen.getAllByText('自定义分析').length).toBeGreaterThan(0)
    // Create form: scope, targets, question, original-text policy, force.
    fireEvent.change(screen.getByLabelText('章节范围'), { target: { value: '1-10' } })
    fireEvent.change(screen.getByLabelText('目标元素'), { target: { value: '角色:林婉' } })
    fireEvent.change(screen.getByLabelText('自定义问题'), { target: { value: '关系如何' } })
    fireEvent.change(screen.getByLabelText('原文策略'), { target: { value: 'none' } })
    fireEvent.click(screen.getByLabelText('强制重跑'))
    fireEvent.click(screen.getByRole('button', { name: '创建分析任务' }))
    await waitFor(() => expect(fake.calls.tasksCreate).toHaveLength(1))
    expect(fake.calls.tasksCreate[0]).toEqual({
      type: 'analysis',
      params: { task: 'summary', chapters: '1-10', targets: ['角色:林婉'], question: '关系如何', original_text: 'none', force: true },
    })
  })

  it('chapters tab: multi-select + four batch actions', async () => {
    await selectProject()
    goTab('章节')
    // Two chapter rows selectable.
    const checks = screen.getAllByRole('checkbox', { name: /选择章节/ })
    expect(checks).toHaveLength(2)
    fireEvent.click(checks[0])
    fireEvent.click(checks[1])
    // Four batch actions.
    for (const label of ['重跑章节分析', '重跑 Delta', '生成分章视觉资产', '生成分章叙事结构']) {
      expect(screen.getByRole('button', { name: label })).toBeEnabled()
    }
    fireEvent.click(screen.getByRole('button', { name: '重跑章节分析' }))
    await waitFor(() => expect(fake.calls.tasksCreate).toHaveLength(1))
    expect(fake.calls.tasksCreate[0].type).toBe('chapter_analysis_rerun')
    expect(fake.calls.tasksCreate[0].params.chapters).toEqual([1, 2])
  })

  it('governance tab: range selection + history', async () => {
    await selectProject()
    goTab('质量治理')
    expect(screen.getByLabelText('审计范围')).toBeInTheDocument()
    expect(screen.getByText('治理历史')).toBeInTheDocument()
    // History shows the failed governance task (badge title + error cell).
    expect(screen.getAllByText(/校验失败/).length).toBeGreaterThan(0)
    fireEvent.change(screen.getByLabelText('审计范围'), { target: { value: 'range' } })
    fireEvent.change(screen.getByLabelText('章节区间'), { target: { value: '101-150' } })
    fireEvent.click(screen.getByText('启动主动审计'))
    await waitFor(() => expect(fake.calls.tasksCreate).toHaveLength(1))
    expect(fake.calls.tasksCreate[0]).toEqual({
      type: 'governance',
      params: { chapters: '101-150', force: false, auto_apply: true },
    })
  })

  it('export tab: source + single Markdown/Vault/Dataview/Canvas + force', async () => {
    await selectProject()
    goTab('导出')
    expect(screen.getByLabelText('数据源')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('数据源'), { target: { value: 'process' } })
    for (const kind of ['single', 'vault', 'dataview', 'canvas']) {
      fireEvent.click(screen.getByLabelText(`输出_${kind}`))
    }
    fireEvent.click(screen.getByLabelText('强制重新导出'))
    fireEvent.click(screen.getByText('创建导出任务'))
    await waitFor(() => expect(fake.calls.tasksCreate).toHaveLength(1))
    expect(fake.calls.tasksCreate[0]).toEqual({
      type: 'export',
      params: { source: 'process', kinds: ['single', 'vault', 'dataview', 'canvas'], force: true },
    })
  })

  it('overview: append preview / conflict / apply', async () => {
    await selectProject()
    goTab('概览')
    fireEvent.change(screen.getByLabelText('源书路径'), { target: { value: 'C:/books/v2.txt' } })
    fireEvent.click(screen.getByText('检查新增章节'))
    await waitFor(() => expect(fake.calls.appendPreview).toBe(1))
    expect(screen.getByText(/第三章 转/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('执行追加'))
    await waitFor(() => expect(fake.calls.apply).toBe(1))
  })

  it('overview: append conflict blocks apply and shows diff', async () => {
    fake.setAppendPreview({ conflict: { reason: '标题前缀不匹配', mismatch: ['第一章'] } })
    await selectProject()
    goTab('概览')
    fireEvent.change(screen.getByLabelText('源书路径'), { target: { value: 'C:/books/v2.txt' } })
    fireEvent.click(screen.getByText('检查新增章节'))
    await waitFor(() => expect(fake.calls.appendPreview).toBe(1))
    expect(screen.getByText(/标题前缀不匹配/)).toBeInTheDocument()
    // Apply is blocked when there is a conflict.
    expect(screen.getByText('执行追加')).toBeDisabled()
  })

  it('task events refresh only the selected project', async () => {
    await selectProject()
    // Wait for the hook's initial load to settle so we don't race the
    // in-flight refresh against the emitted events below.
    await waitFor(() => expect(fake.calls.tasksList).toBeGreaterThanOrEqual(1))
    await waitFor(() => expect(fake.calls.artifactsStatus).toBeGreaterThanOrEqual(1))
    const listBefore = fake.calls.tasksList
    const statusBefore = fake.calls.artifactsStatus
    // Event for a different project: must not re-fetch.
    fake.emit({ type: 'task_done', project_id: 999, task_id: 1 })
    await waitFor(() => expect(fake.calls.tasksList).toBe(listBefore))
    expect(fake.calls.artifactsStatus).toBe(statusBefore)
    // Event for the selected project: re-fetch.
    fake.emit({ type: 'task_done', project_id: 1, task_id: 10 })
    await waitFor(() => expect(fake.calls.tasksList).toBe(listBefore + 1))
    expect(fake.calls.artifactsStatus).toBe(statusBefore + 1)
  })

  it('artifact and log paths open in the drawer', async () => {
    await selectProject()
    goTab('章节')
    // Click an artifact preview button on a chapter row.
    fireEvent.click(screen.getAllByText('分析')[0])
    await waitFor(() => expect(fake.calls.artifact).toBe(1))
    expect(screen.getByText('产物内容预览')).toBeInTheDocument()
    goTab('日志')
    fireEvent.click(await screen.findByText('worker.log'))
    await waitFor(() => expect(fake.calls.log).toBe(1))
    expect(screen.getByText('worker 日志内容')).toBeInTheDocument()
  })
})
