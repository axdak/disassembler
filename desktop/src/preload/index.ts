import { contextBridge, ipcRenderer } from 'electron'

const api = {
  listProjects: () => ipcRenderer.invoke('nd:projects:list'),
  createProject: (path: string, name?: string) =>
    ipcRenderer.invoke('nd:projects:create', { path, name }),
  projectStatus: (id: number) => ipcRenderer.invoke('nd:projects:status', id),
  deleteProject: (id: number) => ipcRenderer.invoke('nd:projects:delete', id),
  startRun: (id: number, config?: Record<string, unknown>) =>
    ipcRenderer.invoke('nd:projects:run', id, config),
  cancelRun: (id: number) => ipcRenderer.invoke('nd:projects:cancel', id),
  getSettings: () => ipcRenderer.invoke('nd:settings:get'),
  updateSettings: (settings: Record<string, unknown>) =>
    ipcRenderer.invoke('nd:settings:update', settings),
  onEvent: (cb: (ev: unknown) => void) => {
    const handler = (_: unknown, ev: unknown) => cb(ev)
    ipcRenderer.on('nd:event', handler)
    return () => ipcRenderer.removeListener('nd:event', handler)
  },
}

contextBridge.exposeInMainWorld('api', api)

export type BackendApi = typeof api
