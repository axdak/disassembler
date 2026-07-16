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
