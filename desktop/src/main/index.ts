import { app, BrowserWindow, dialog, ipcMain, Menu, Tray, nativeImage, shell } from 'electron'
import { join } from 'node:path'
import type { ChildProcessByStdio } from 'child_process'
import type { Readable } from 'node:stream'
import WebSocket from 'ws'
import { startBackendProcess, terminateBackendProcess } from './backend-process'
import { parseBackendResponse } from './http'

let backendProc: ChildProcessByStdio<null, Readable, Readable> | null = null
let backendBase = ''
let backendToken = ''
let isQuitting = false
let tray: Tray | null = null

function startBackend(): Promise<void> {
  // Spawn synchronously so the handle is available for before-quit kill even
  // during the startup window (before BACKEND_READY fires); Windows does not
  // auto-reap an orphaned Python child.
  const { proc, ready } = startBackendProcess()
  backendProc = proc
  return ready.then((h) => {
    backendBase = h.base
    backendToken = h.token
  })
}

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${backendToken}` }
}

async function bget(p: string): Promise<any> {
  if (process.env.NDD_DEBUG) process.stdout.write(`[api] GET ${p}\n`)
  const r = await fetch(`${backendBase}${p}`, { headers: authHeaders() })
  return parseBackendResponse(r)
}
async function bpost(p: string, body?: any): Promise<any> {
  const r = await fetch(`${backendBase}${p}`, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : '{}',
  })
  return parseBackendResponse(r)
}
async function bdel(p: string): Promise<any> {
  const r = await fetch(`${backendBase}${p}`, { method: 'DELETE', headers: authHeaders() })
  return parseBackendResponse(r)
}

function registerIpc(): void {
  ipcMain.handle('nd:projects:list', () => bget('/projects'))
  ipcMain.handle('nd:projects:create', (_e, body: { path: string; name?: string }) => bpost('/projects', body))
  ipcMain.handle('nd:projects:status', (_e, id: number) => bget(`/projects/${id}`))
  // Keep each project's run limit in the backend database across desktop restarts.
  ipcMain.handle('nd:projects:runRange', (_e, id: number, endChapterSeq: number) =>
    fetch(`${backendBase}/projects/${id}/run-range`, {
      method: 'PUT',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ end_chapter_seq: endChapterSeq }),
    }).then(parseBackendResponse))
  ipcMain.handle('nd:projects:delete', (_e, id: number) => bdel(`/projects/${id}`))
  ipcMain.handle('nd:projects:run', (_e, id: number, config?: Record<string, unknown>) =>
    bpost(`/projects/${id}/run`, { config: config || {} }))
  ipcMain.handle('nd:projects:cancel', (_e, id: number) => bpost(`/projects/${id}/cancel`, {}))
  ipcMain.handle('nd:projects:chapters', (_e, id: number) => bget(`/projects/${id}/chapters`))
  // Export keeps the final structure intact and writes a separate Storyer-ready JSON.
  ipcMain.handle('nd:projects:exportStoryer', (_e, id: number) =>
    bpost(`/projects/${id}/export/storyer`, {}))
  ipcMain.handle('nd:projects:artifact', (_e, id: number, path: string) =>
    bget(`/projects/${id}/artifact?path=${encodeURIComponent(path)}`))
  // Workbench tasks: create/list/get/cancel/retry
  ipcMain.handle('nd:projects:tasks:create', (_e, id: number, type: string, params: Record<string, unknown>) =>
    bpost(`/projects/${id}/tasks`, { type, params }))
  ipcMain.handle('nd:projects:tasks:list', (_e, id: number) => bget(`/projects/${id}/tasks`))
  ipcMain.handle('nd:projects:task:get', (_e, id: number, taskId: number) =>
    bget(`/projects/${id}/tasks/${taskId}`))
  ipcMain.handle('nd:projects:task:cancel', (_e, id: number, taskId: number) =>
    bpost(`/projects/${id}/tasks/${taskId}/cancel`, {}))
  ipcMain.handle('nd:projects:task:retry', (_e, id: number, taskId: number) =>
    bpost(`/projects/${id}/tasks/${taskId}/retry`, {}))
  // Workbench artifact status + chapter append flow
  ipcMain.handle('nd:projects:artifacts:status', (_e, id: number) =>
    bget(`/projects/${id}/artifacts/status`))
  ipcMain.handle('nd:projects:chapters:appendPreview',
    (_e, id: number, body: { source_file: string; pattern?: string; increment_only?: boolean }) =>
      bpost(`/projects/${id}/chapters/append-preview`, body))
  ipcMain.handle('nd:projects:chapters:apply', (_e, id: number, plan: Record<string, unknown>) =>
    bpost(`/projects/${id}/chapters/apply`, { plan }))
  ipcMain.handle('nd:projects:recoverSnapshots', (_e, id: number) =>
    bget(`/projects/${id}/recover/snapshots`))
  ipcMain.handle('nd:projects:logs', (_e, id: number) => bget(`/projects/${id}/logs`))
  ipcMain.handle('nd:projects:log', (_e, id: number, name: string) =>
    bget(`/projects/${id}/logs/${encodeURIComponent(name)}`))
  ipcMain.handle('nd:settings:get', () => bget('/settings'))
  ipcMain.handle('nd:settings:concurrency', () => bget('/settings/concurrency'))
  ipcMain.handle('nd:settings:update', (_e, settings: Record<string, unknown>) =>
    fetch(`${backendBase}/settings`, {
      method: 'PUT',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    }).then(parseBackendResponse))
  ipcMain.handle('nd:queue:add', (_e, id: number) => bpost('/queue', { project_id: id }))
  ipcMain.handle('nd:queue:list', () => bget('/queue'))
  ipcMain.handle('nd:queue:remove', (_e, position: number) => bdel(`/queue/${position}`))
  ipcMain.handle('nd:queue:start', () => bpost('/queue/start'))
  ipcMain.handle('nd:queue:stop', () => bpost('/queue/stop'))
  ipcMain.handle('nd:dialog:pickBook', async () => {
    const r = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: [{ name: '书 (txt/docx)', extensions: ['txt', 'docx', 'doc'] }],
    })
    return r.canceled ? null : r.filePaths[0]
  })
  ipcMain.handle('nd:dialog:pickFolder', async () => {
    const r = await dialog.showOpenDialog({ properties: ['openDirectory'] })
    return r.canceled ? null : r.filePaths[0]
  })
  ipcMain.handle('nd:projects:fromBook', (_e, source_file: string) =>
    bpost('/projects/from-book', { source_file }))
  // 在系统文件管理器中打开并选中指定文件
  ipcMain.handle('nd:open-in-folder', (_e, path: string) => {
    shell.showItemInFolder(path)
    return true
  })
}

function startEventBridge(win: BrowserWindow): void {
  const url = `${backendBase.replace(/^http/, 'ws')}/ws/events?token=${encodeURIComponent(backendToken)}`
  // uvicorn 起服有窗口期，连接被拒则 1s 后重连，直到连上
  const connect = () => {
    if (isQuitting || win.isDestroyed()) return
    const ws = new WebSocket(url)
    ws.on('open', () => {
      // A reconnect may have missed terminal events. Ask views to reconcile once
      // instead of reintroducing periodic scans as a connectivity workaround.
      if (!win.isDestroyed()) win.webContents.send('nd:event', { type: 'connection_restored' })
    })
    ws.on('message', (data: any) => {
      if (win.isDestroyed()) return
      try {
        win.webContents.send('nd:event', JSON.parse(data.toString()))
      } catch (e) {
        process.stderr.write(`[ws] parse error: ${e}\n`)
      }
    })
    ws.on('close', () => {
      if (!isQuitting && !win.isDestroyed()) setTimeout(connect, 1000)
    })
    ws.on('error', (e) => process.stderr.write(`[ws] error: ${e}\n`))
  }
  connect()
}

function loadTrayIcon(): Electron.NativeImage {
  const p = join(__dirname, '../../assets/icon.png')
  const img = nativeImage.createFromPath(p)
  return img.isEmpty() ? nativeImage.createEmpty() : img
}

function createTray(win: BrowserWindow): void {
  tray = new Tray(loadTrayIcon())
  tray.setToolTip('Novel Disassembler')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '显示窗口', click: () => { win.show(); win.focus() } },
      { type: 'separator' },
      { label: '退出', click: () => { isQuitting = true; app.quit() } },
    ])
  )
  tray.on('click', () => { win.show(); win.focus() })
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1200,
    height: 820,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  })
  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
  // Never expose the privileged preload bridge to navigated external content.
  // External links open in the system browser and popup windows remain disabled.
  win.webContents.on('will-navigate', (event, targetUrl) => {
    const currentUrl = win.webContents.getURL()
    if (currentUrl && targetUrl.split('#')[0] !== currentUrl.split('#')[0]) {
      event.preventDefault()
      if (/^https?:\/\//i.test(targetUrl)) void shell.openExternal(targetUrl)
    }
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  // 关闭缩到托盘（非退出时）
  win.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault()
      win.hide()
    }
  })
  createTray(win)
  return win
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const wins = BrowserWindow.getAllWindows()
    if (wins[0]) { wins[0].isMinimized() && wins[0].restore(); wins[0].show(); wins[0].focus() }
  })

  app.whenReady().then(async () => {
    try {
      await startBackend()
      process.stdout.write(`[main] backend ready at ${backendBase}\n`)
    } catch (e: any) {
      process.stderr.write(`[main] backend failed: ${e?.message}\n`)
      dialog.showErrorBox('后端启动失败', e?.message || '无法启动本地后端服务')
      app.quit()
      return
    }
    registerIpc()
    const win = createWindow()
    startEventBridge(win)
  })
}

app.on('window-all-closed', () => {
  // 托盘模式下不退出；无托盘时退出
})
app.on('before-quit', () => {
  isQuitting = true
  if (backendProc) { terminateBackendProcess(backendProc); backendProc = null }
})
