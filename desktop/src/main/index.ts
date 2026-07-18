import { app, BrowserWindow, dialog, ipcMain, Menu, Tray, nativeImage } from 'electron'
import { join, resolve } from 'path'
import { spawn, type ChildProcessByStdio } from 'child_process'
import type { Readable } from 'node:stream'
import WebSocket from 'ws'

// dev: out/main/index.js -> desktop -> 仓库根
const REPO_ROOT = resolve(__dirname, '../../..')
const VENV_PYTHON = join(REPO_ROOT, 'runtime/venv/Scripts/python.exe')
const BACKEND_MAIN = join(REPO_ROOT, 'backend/main.py')

let backendProc: ChildProcessByStdio<null, Readable, Readable> | null = null
let backendBase = ''
let backendToken = ''
let isQuitting = false
let tray: Tray | null = null

function startBackend(): Promise<void> {
  return new Promise((resolveP, rejectP) => {
    const env = {
      ...process.env,
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
    }
    const proc = spawn(VENV_PYTHON, ['-u', BACKEND_MAIN, '--port', '0'], {
      env, stdio: ['ignore', 'pipe', 'pipe'],
    })
    backendProc = proc
    let buf = ''
    let resolved = false
    const timer = setTimeout(() => {
      if (!resolved) { resolved = true; rejectP(new Error('backend startup timeout')) }
    }, 30000)
    const onData = (chunk: Buffer) => {
      buf += chunk.toString('utf8')
      let idx: number
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).trim()
        buf = buf.slice(idx + 1)
        if (line) process.stdout.write(`[backend] ${line}\n`)
        const m = line.match(/BACKEND_READY host=(\S+) port=(\d+) token=(\S+)/)
        if (m && !resolved) {
          resolved = true
          clearTimeout(timer)
          backendBase = `http://${m[1]}:${m[2]}`
          backendToken = m[3]
          resolveP()
        }
      }
    }
    proc.stdout.on('data', onData)
    proc.stderr.on('data', (d) => process.stderr.write(`[backend err] ${d.toString('utf8')}`))
    proc.on('exit', (code) => {
      process.stderr.write(`[backend] exited code=${code}\n`)
      if (!resolved) { resolved = true; clearTimeout(timer); rejectP(new Error(`backend exited code=${code}`)) }
    })
  })
}

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${backendToken}` }
}

async function bget(p: string): Promise<any> {
  if (process.env.NDD_DEBUG) process.stdout.write(`[api] GET ${p}\n`)
  const r = await fetch(`${backendBase}${p}`, { headers: authHeaders() })
  return r.json()
}
async function bpost(p: string, body?: any): Promise<any> {
  const r = await fetch(`${backendBase}${p}`, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : '{}',
  })
  return r.json()
}
async function bdel(p: string): Promise<any> {
  const r = await fetch(`${backendBase}${p}`, { method: 'DELETE', headers: authHeaders() })
  return r.json()
}

function registerIpc(): void {
  ipcMain.handle('nd:projects:list', () => bget('/projects'))
  ipcMain.handle('nd:projects:create', (_e, body: { path: string; name?: string }) => bpost('/projects', body))
  ipcMain.handle('nd:projects:status', (_e, id: number) => bget(`/projects/${id}`))
  ipcMain.handle('nd:projects:delete', (_e, id: number) => bdel(`/projects/${id}`))
  ipcMain.handle('nd:projects:run', (_e, id: number, config?: Record<string, unknown>) =>
    bpost(`/projects/${id}/run`, { config: config || {} }))
  ipcMain.handle('nd:projects:cancel', (_e, id: number) => bpost(`/projects/${id}/cancel`, {}))
  ipcMain.handle('nd:projects:chapters', (_e, id: number) => bget(`/projects/${id}/chapters`))
  ipcMain.handle('nd:projects:artifact', (_e, id: number, path: string) =>
    bget(`/projects/${id}/artifact?path=${encodeURIComponent(path)}`))
  ipcMain.handle('nd:projects:logs', (_e, id: number) => bget(`/projects/${id}/logs`))
  ipcMain.handle('nd:projects:log', (_e, id: number, name: string) =>
    bget(`/projects/${id}/logs/${encodeURIComponent(name)}`))
  ipcMain.handle('nd:settings:get', () => bget('/settings'))
  ipcMain.handle('nd:settings:update', (_e, settings: Record<string, unknown>) =>
    fetch(`${backendBase}/settings`, {
      method: 'PUT',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    }).then((r) => r.json()))
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
}

function startEventBridge(win: BrowserWindow): void {
  const url = `${backendBase.replace(/^http/, 'ws')}/ws/events?token=${encodeURIComponent(backendToken)}`
  // uvicorn 起服有窗口期，连接被拒则 1s 后重连，直到连上
  const connect = () => {
    const ws = new WebSocket(url)
    ws.on('message', (data: any) => {
      if (win.isDestroyed()) return
      try {
        win.webContents.send('nd:event', JSON.parse(data.toString()))
      } catch (e) {
        process.stderr.write(`[ws] parse error: ${e}\n`)
      }
    })
    ws.on('close', () => setTimeout(connect, 1000))
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
      sandbox: false,
    },
  })
  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
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
  if (backendProc) { backendProc.kill(); backendProc = null }
})
