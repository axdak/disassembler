// renderer 无 Node 集成；通过 preload contextBridge 暴露的 window.api 访问后端白名单。
// 类型由 Task 6 引入的 renderer 共享类型 (./types.ts) 收紧。本文件以
// `declare global` 形式合并到全局 `Window`，使渲染层 `window.api` 调用有强类型。
import type {
  Project,
  Status,
  Chapter,
  Artifact,
  LogEntry,
  QueueItem,
  WorkbenchTask,
  ArtifactStatusResponse,
  AppendPlan,
  RecoverSnapshot,
} from './types'

declare global {
  interface Window {
    api: {
      listProjects(): Promise<Project[]>
      createProject(path: string, name?: string): Promise<Project>
      projectStatus(id: number): Promise<Status>
      updateProjectRunRange(id: number, endChapterSeq: number): Promise<Project>
      deleteProject(id: number): Promise<{ ok: boolean }>
      startRun(id: number, config?: Record<string, unknown>): Promise<{ run_id: number }>
      cancelRun(id: number): Promise<{ cancelled: boolean }>
      chapters(id: number): Promise<Chapter[]>
      exportStoryer(id: number): Promise<{ path: string; chapter_count: number; missing_analysis: number }>
      artifact(id: number, path: string): Promise<Artifact>
      tasksCreate(id: number, type: string, params: Record<string, unknown>): Promise<WorkbenchTask>
      tasksList(id: number): Promise<WorkbenchTask[]>
      taskGet(id: number, taskId: number): Promise<WorkbenchTask>
      taskCancel(id: number, taskId: number): Promise<WorkbenchTask>
      taskRetry(id: number, taskId: number): Promise<WorkbenchTask>
      artifactsStatus(id: number): Promise<ArtifactStatusResponse>
      chaptersAppendPreview(
        id: number,
        body: { source_file: string; pattern?: string; increment_only?: boolean },
      ): Promise<AppendPlan>
      chaptersApply(id: number, plan: Record<string, unknown>): Promise<{ applied?: unknown }>
      recoverSnapshots(id: number): Promise<RecoverSnapshot[]>
      logs(id: number): Promise<LogEntry[]>
      log(id: number, name: string): Promise<{ content: string }>
      getSettings(): Promise<Record<string, unknown>>
      updateSettings(s: Record<string, unknown>): Promise<Record<string, unknown>>
      queueAdd(id: number): Promise<{ position: number }>
      queueList(): Promise<QueueItem[]>
      queueRemove(position: number): Promise<{ removed: boolean }>
      queueStart(): Promise<{ started: boolean }>
      queueStop(): Promise<{ stopped: boolean }>
      pickBook(): Promise<string | null>
      pickFolder(): Promise<string | null>
      createFromBook(source_file: string): Promise<Project>
      onEvent(cb: (ev: unknown) => void): () => void
      openInFolder(path: string): Promise<void>
    }
  }
}

export {}
