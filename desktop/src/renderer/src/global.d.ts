// renderer 无 Node 集成；通过 preload contextBridge 暴露的 window.api 访问后端白名单
interface Window {
  api: {
    listProjects(): Promise<unknown>
    createProject(path: string, name?: string): Promise<unknown>
    projectStatus(id: number): Promise<unknown>
    deleteProject(id: number): Promise<{ ok: boolean }>
    startRun(id: number, config?: Record<string, unknown>): Promise<{ run_id: number }>
    cancelRun(id: number): Promise<{ cancelled: boolean }>
    getSettings(): Promise<Record<string, unknown>>
    updateSettings(s: Record<string, unknown>): Promise<Record<string, unknown>>
    onEvent(cb: (ev: unknown) => void): () => void
  }
}
