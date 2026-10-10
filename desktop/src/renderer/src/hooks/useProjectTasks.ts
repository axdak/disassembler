// useProjectTasks
//
// Loads workbench tasks + artifact status for the currently-selected project
// and subscribes to project-scoped task/pipeline WebSocket events. Chapter
// browsing uses manual artifact refreshes to avoid repeated whole-book scans.
// Task events still update cancellation controls without rescanning artifacts.
//
// Exposes `create`/`cancel`/`retry` actions and surfaces concise Chinese
// error strings. The renderer-level banner (in App.tsx) attaches the link to
// the logs/artifact drawer.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ArtifactStatusResponse, TaskType, WorkbenchTask } from '../types'

const TASK_EVENT_PREFIX = 'task_'
const PIPELINE_ARTIFACT_EVENTS = new Set([
  'chapter_progress', 'stage_done', 'stage_handoff', 'pipeline_done', 'pipeline_blocked',
])

/** Build a concise Chinese error string from a thrown value. */
export function messageForError(e: unknown, prefix: string): string {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  const tail = raw ? `：${raw}` : ''
  return `${prefix}${tail}`
}

export interface ProjectTasks {
  tasks: WorkbenchTask[]
  artifactStatus: ArtifactStatusResponse | null
  lastArtifactRefreshAt: number | null
  loading: boolean
  error: string | null
  create: (type: TaskType, params: Record<string, unknown>) => Promise<void>
  cancel: (taskId: number) => Promise<void>
  retry: (taskId: number) => Promise<void>
  refresh: () => Promise<void>
  refreshArtifacts: () => Promise<void>
  clearError: () => void
}

export function useProjectTasks(projectId: number | null, manualArtifacts = false): ProjectTasks {
  const [tasks, setTasks] = useState<WorkbenchTask[]>([])
  const [artifactStatus, setArtifactStatus] = useState<ArtifactStatusResponse | null>(null)
  const [lastArtifactRefreshAt, setLastArtifactRefreshAt] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const artifactRequest = useRef(0)
  const currentProject = useRef(projectId)
  currentProject.current = projectId
  // Switching tabs changes event handling without reloading the entire book.
  const manualArtifactsRef = useRef(manualArtifacts)
  manualArtifactsRef.current = manualArtifacts

  /** Read file-derived cards for an explicit refresh or an eligible event. */
  const refreshArtifacts = useCallback(async () => {
    if (projectId == null) return
    const request = ++artifactRequest.current
    try {
      const snapshot = await window.api.artifactsStatus(projectId)
      if (request === artifactRequest.current && currentProject.current === projectId) {
        setArtifactStatus(snapshot)
        setLastArtifactRefreshAt(Date.now())
        setError((previous) => previous?.startsWith('加载产物状态失败') ? null : previous)
      }
    } catch (e) {
      if (request === artifactRequest.current && currentProject.current === projectId) {
        setError((previous) => previous && !previous.startsWith('加载产物状态失败')
          ? previous : messageForError(e, '加载产物状态失败'))
      }
    }
  }, [projectId])

  const refresh = useCallback(async () => {
    if (projectId == null) return
    const request = ++artifactRequest.current
    setLoading(true)
    try {
      const [t, s] = await Promise.all([
        window.api.tasksList(projectId),
        window.api.artifactsStatus(projectId),
      ])
      if (currentProject.current === projectId) {
        setTasks(t)
        if (request === artifactRequest.current) {
          setArtifactStatus(s)
          setLastArtifactRefreshAt(Date.now())
        }
        setError(null)
      }
    } catch (e) {
      if (currentProject.current === projectId) setError(messageForError(e, '加载任务失败'))
    } finally {
      if (currentProject.current === projectId) setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (projectId == null) {
      artifactRequest.current++
      setTasks([])
      setArtifactStatus(null)
      setLastArtifactRefreshAt(null)
      setError(null)
      return
    }
    refresh()
    // 章节浏览期间仅同步任务控制状态，产物由手动刷新读取。
    const off = window.api.onEvent((ev) => {
      if (ev == null || typeof ev !== 'object') return
      const e = ev as { project_id?: number; type?: string }
      if (e.project_id !== projectId) return
      if (typeof e.type !== 'string') return
      if (e.type.startsWith(TASK_EVENT_PREFIX)) {
        if (!manualArtifactsRef.current) void refresh()
        else void window.api.tasksList(projectId).then((items) => {
          if (currentProject.current === projectId) setTasks(items)
        }).catch((error) => {
          if (currentProject.current === projectId) setError(messageForError(error, '加载任务失败'))
        })
      } else if (!manualArtifactsRef.current && PIPELINE_ARTIFACT_EVENTS.has(e.type)) {
        void refreshArtifacts()
      }
    })
    return () => { artifactRequest.current++; off() }
  }, [projectId, refresh, refreshArtifacts])

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

  return { tasks, artifactStatus, lastArtifactRefreshAt, loading, error,
    create, cancel, retry, refresh, refreshArtifacts, clearError }
}
