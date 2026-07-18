// useProjectTasks
//
// Loads workbench tasks + artifact status for the currently-selected project
// and subscribes to `task_*` WebSocket events via `window.api.onEvent`. Events
// refresh ONLY the selected project: any event whose `project_id` does not
// match the active project is ignored, so switching projects (or activity on
// a different project) never triggers a re-fetch here.
//
// Exposes `create`/`cancel`/`retry` actions and surfaces concise Chinese
// error strings. The renderer-level banner (in App.tsx) attaches the link to
// the logs/artifact drawer.
import { useCallback, useEffect, useState } from 'react'
import type { ArtifactStatusResponse, TaskType, WorkbenchTask } from '../types'

const TASK_EVENT_PREFIX = 'task_'

/** Build a concise Chinese error string from a thrown value. */
export function messageForError(e: unknown, prefix: string): string {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  const tail = raw ? `：${raw}` : ''
  return `${prefix}${tail}`
}

export interface ProjectTasks {
  tasks: WorkbenchTask[]
  artifactStatus: ArtifactStatusResponse | null
  loading: boolean
  error: string | null
  create: (type: TaskType, params: Record<string, unknown>) => Promise<void>
  cancel: (taskId: number) => Promise<void>
  retry: (taskId: number) => Promise<void>
  refresh: () => Promise<void>
  clearError: () => void
}

export function useProjectTasks(projectId: number | null): ProjectTasks {
  const [tasks, setTasks] = useState<WorkbenchTask[]>([])
  const [artifactStatus, setArtifactStatus] = useState<ArtifactStatusResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (projectId == null) return
    setLoading(true)
    try {
      const [t, s] = await Promise.all([
        window.api.tasksList(projectId),
        window.api.artifactsStatus(projectId),
      ])
      setTasks(t)
      setArtifactStatus(s)
      setError(null)
    } catch (e) {
      setError(messageForError(e, '加载任务失败'))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (projectId == null) {
      setTasks([])
      setArtifactStatus(null)
      setError(null)
      return
    }
    refresh()
    // Subscribe to task_* events; only refresh when the event's project_id
    // matches the currently-selected project.
    const off = window.api.onEvent((ev) => {
      if (ev == null || typeof ev !== 'object') return
      const e = ev as { project_id?: number; type?: string }
      if (e.project_id !== projectId) return
      if (typeof e.type !== 'string' || !e.type.startsWith(TASK_EVENT_PREFIX)) return
      refresh()
    })
    return () => off()
  }, [projectId, refresh])

  const create = useCallback(async (type: TaskType, params: Record<string, unknown>) => {
    if (projectId == null) return
    try {
      await window.api.tasksCreate(projectId, type, params)
      await refresh()
    } catch (e) {
      setError(messageForError(e, '创建任务失败'))
    }
  }, [projectId, refresh])

  const cancel = useCallback(async (taskId: number) => {
    if (projectId == null) return
    try {
      await window.api.taskCancel(projectId, taskId)
      await refresh()
    } catch (e) {
      setError(messageForError(e, '取消任务失败'))
    }
  }, [projectId, refresh])

  const retry = useCallback(async (taskId: number) => {
    if (projectId == null) return
    try {
      await window.api.taskRetry(projectId, taskId)
      await refresh()
    } catch (e) {
      setError(messageForError(e, '重试任务失败'))
    }
  }, [projectId, refresh])

  const clearError = useCallback(() => setError(null), [])

  return { tasks, artifactStatus, loading, error, create, cancel, retry, refresh, clearError }
}
