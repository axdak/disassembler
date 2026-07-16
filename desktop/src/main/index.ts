import { app, BrowserWindow, ipcMain } from 'electron'
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

function startBackend(): Promise<void> {
  return new Promise((resolveP, rejectP) => {
    const env = {
      ...process.env,
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONUTF8: '1',
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
    proc.stderr.on('data', (d) => process.stderr.write(`[backend err] ${d}`))
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
  ipcMain.handle('nd:settings:get', () => bget('/settings'))
  ipcMain.handle('nd:settings:update', (_e, settings: Record<string, unknown>) =>
    fetch(`${backendBase}/settings`, {
      method: 'PUT',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    }).then((r) => r.json()))
}

function startEventBridge(win: BrowserWindow): void {
  const url = `${backendBase.replace(/^http/, 'ws')}/ws/events?token=${encodeURIComponent(backendToken)}`
  const ws = new WebSocket(url)
  ws.on('message', (data: any) => {
    try {
      win.webContents.send('nd:event', JSON.parse(data.toString()))
    } catch (e) {
      process.stderr.write(`[ws] parse error: ${e}\n`)
    }
  })
  ws.on('close', () => process.stderr.write('[ws] closed\n'))
  ws.on('error', (e) => process.stderr.write(`[ws] error: ${e}\n`))
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1100,
    height: 760,
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
  return win
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const wins = BrowserWindow.getAllWindows()
    if (wins[0]) { wins[0].isMinimized() && wins[0].restore(); wins[0].focus() }
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
  if (backendProc) { backendProc.kill(); backendProc = null }
  app.quit()
})
app.on('before-quit', () => {
  if (backendProc) { backendProc.kill(); backendProc = null }
})
