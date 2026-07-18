export interface Project {
  id: number
  name: string
  path: string
  created_at: string
  updated_at: string
}

export interface StageEvent {
  id: number
  run_id: number | null
  project_id: number | null
  type: string
  stage?: string
  detail?: string
  data: Record<string, unknown>
  ts: string
}

export interface Status {
  project: Project
  active_run: { id: number; status: string; rc: number | null } | null
  current_stage: string | null
  completed: string[]
  last_error: string | null
  recent_events: StageEvent[]
  // Independent-workbench additions (mirror backend/models.py Status).
  revisions: Record<string, string>
  task_summary: Record<string, number>
  stale_task_count: number
}

export interface Chapter {
  seq: number | null
  filename: string
  title: string
  has_analysis: boolean
  has_delta: boolean
}

export interface Artifact {
  path: string
  size: number
  truncated: boolean
  content: string
}

export interface LogEntry {
  name: string
  size: number
  mtime: number
}

export interface QueueItem {
  position: number
  project_id: number
}

// Workbench task shapes (mirror backend/models.py WorkbenchTask).
export type TaskType =
  | 'analysis'
  | 'chapter_visual'
  | 'chapter_structure'
  | 'governance'
  | 'export'
  | 'append_chapters'
  | 'chapter_analysis_rerun'
  | 'chapter_delta_rerun'

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface WorkbenchTask {
  id: number
  project_id: number
  task_type: TaskType
  status: TaskStatus
  params: Record<string, unknown>
  input_revision: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  cancel_requested: boolean
  retry_of: number | null
  created_at: string
  updated_at: string
  started_at: string | null
  ended_at: string | null
  stale: boolean
}

// Artifact status card (mirror backend/models.py CardStatus + ArtifactStatusResponse).
export interface CardStatus {
  status:
    | 'missing'
    | 'latest'
    | 'stale'
    | 'queued'
    | 'running'
    | 'failed'
    | 'cancelled'
  artifact_present: boolean
  latest_task_id: number | null
  latest_task_status: string | null
  error: string | null
  paths: string[]
}

export interface ChapterCardStatus {
  seq: number
  filename: string
  title: string
  cards: Record<string, CardStatus>
  stale: boolean
  latest_task: Record<string, unknown> | null
  error: string | null
}

export interface ArtifactStatusResponse {
  dimensions: Record<string, CardStatus>
  chapters: ChapterCardStatus[]
  exports: Record<string, CardStatus>
  counts: Record<string, number>
}

// Chapter append plan returned by append-preview and consumed by apply.
export interface AppendPlan {
  appended?: unknown
  conflict?: Record<string, unknown> | null
  [key: string]: unknown
}
