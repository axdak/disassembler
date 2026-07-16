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
  chapters: (id: number) => ipcRenderer.invoke('nd:projects:chapters', id),
  artifact: (id: number, path: string) => ipcRenderer.invoke('nd:projects:artifact', id, path),
  logs: (id: number) => ipcRenderer.invoke('nd:projects:logs', id),
  log: (id: number, name: string) => ipcRenderer.invoke('nd:projects:log', id, name),
  getSettings: () => ipcRenderer.invoke('nd:settings:get'),
  updateSettings: (settings: Record<string, unknown>) =>
    ipcRenderer.invoke('nd:settings:update', settings),
  queueAdd: (id: number) => ipcRenderer.invoke('nd:queue:add', id),
  queueList: () => ipcRenderer.invoke('nd:queue:list'),
  queueRemove: (position: number) => ipcRenderer.invoke('nd:queue:remove', position),
  queueStart: () => ipcRenderer.invoke('nd:queue:start'),
  queueStop: () => ipcRenderer.invoke('nd:queue:stop'),
  pickBook: () => ipcRenderer.invoke('nd:dialog:pickBook'),
  pickFolder: () => ipcRenderer.invoke('nd:dialog:pickFolder'),
  createFromBook: (source_file: string) => ipcRenderer.invoke('nd:projects:fromBook', source_file),
  onEvent: (cb: (ev: unknown) => void) => {
    const handler = (_: unknown, ev: unknown) => cb(ev)
    ipcRenderer.on('nd:event', handler)
    return () => ipcRenderer.removeListener('nd:event', handler)
  },
}

contextBridge.exposeInMainWorld('api', api)

export type BackendApi = typeof api
