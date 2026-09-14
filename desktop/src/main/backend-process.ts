// Backend spawn + UTF-8 stream decoder helper.
//
// Decoupled from index.ts so the spawn env and stream decode are testable
// without bringing up the real backend. In particular, stdout/stderr are
// decoded with `StringDecoder('utf8')` so multibyte sequences (e.g. Chinese
// characters) that straddle Buffer chunk boundaries are reassembled instead
// of being turned into U+FFFD replacement chars (the prior `buf += chunk`
// bug under code page 936).
import { StringDecoder } from 'node:string_decoder'
import { spawn, spawnSync, type ChildProcessByStdio } from 'child_process'
import { join, resolve } from 'node:path'
import type { Readable } from 'node:stream'

// dev: out/main/index.js -> desktop -> 仓库根
const REPO_ROOT = resolve(__dirname, '../../..')
const VENV_PYTHON = join(REPO_ROOT, 'runtime/venv/Scripts/python.exe')
const BACKEND_MAIN = join(REPO_ROOT, 'backend/main.py')

const BACKEND_READY_RE = /BACKEND_READY host=(\S+) port=(\d+) token=(\S+)/
const STARTUP_TIMEOUT_MS = 30000

export interface BackendHandle {
  base: string
  token: string
  proc: ChildProcessByStdio<null, Readable, Readable>
}

/** A synchronous handle on the spawned backend plus a ready promise.
 *
 * Exposing ``proc`` synchronously lets the caller wire a kill on
 * ``before-quit`` during the startup window (before ``BACKEND_READY`` fires),
 * so quitting mid-startup cannot orphan the Python child on Windows. */
export interface BackendSpawn {
  proc: ChildProcessByStdio<null, Readable, Readable>
  ready: Promise<BackendHandle>
}

/** Env passed to the backend subprocess. Kept here so tests can assert it. */
export function buildBackendEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    NO_PROXY: '127.0.0.1,localhost',
    no_proxy: '127.0.0.1,localhost',
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  }
}

/** Parse a `BACKEND_READY host=<h> port=<p> token=<t>` line. */
export function parseBackendReady(line: string): { base: string; token: string } | null {
  const m = line.match(BACKEND_READY_RE)
  if (!m) return null
  return { base: `http://${m[1]}:${m[2]}`, token: m[3] }
}

/**
 * Stream decoded UTF-8 text from a Buffer Readable. Uses StringDecoder so a
 * multibyte sequence split across chunks is buffered until complete rather
 * than emitted as U+FFFD. Returns a disposer that removes the listeners.
 */
export function streamUtf8(stream: Readable, onText: (text: string) => void): () => void {
  const decoder = new StringDecoder('utf8')
  const onData = (chunk: Buffer) => onText(decoder.write(chunk))
  const onEnd = () => {
    const rest = decoder.end()
    if (rest) onText(rest)
  }
  stream.on('data', onData)
  stream.on('end', onEnd)
  return () => {
    stream.removeListener('data', onData)
    stream.removeListener('end', onEnd)
  }
}


/** Stop the backend and its worker descendants when the desktop app exits. */
export function terminateBackendProcess(
  proc: ChildProcessByStdio<null, Readable, Readable>,
): void {
  if (proc.exitCode !== null || proc.signalCode !== null) return
  if (process.platform === 'win32' && proc.pid) {
    // Windows does not propagate ChildProcess.kill() to grandchildren. taskkill
    // targets the exact spawned PID and closes its worker process tree.
    spawnSync('taskkill', ['/PID', String(proc.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    })
    return
  }
  proc.kill()
}

/** Spawn the venv backend, decode its stdio, and resolve once BACKEND_READY fires.
 *
 * Returns the spawned ``proc`` synchronously (so the caller can kill it during
 * the startup window) plus a ``ready`` promise that resolves with the decoded
 * base/token handle once the ``BACKEND_READY`` handshake completes. Preserves
 * the 30s startup timeout and the ready-handshake/env behavior. */
export function startBackendProcess(): BackendSpawn {
  const proc = spawn(VENV_PYTHON, ['-u', BACKEND_MAIN, '--port', '0'], {
    env: buildBackendEnv(),
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  const ready = new Promise<BackendHandle>((resolveP, rejectP) => {
    let resolved = false
    let stdoutBuf = ''
    const timer = setTimeout(() => {
      if (!resolved) {
        resolved = true
        proc.kill()
        rejectP(new Error('backend startup timeout'))
      }
    }, STARTUP_TIMEOUT_MS)

    streamUtf8(proc.stdout, (text) => {
      stdoutBuf += text
      let idx: number
      while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
        const line = stdoutBuf.slice(0, idx).replace(/\r$/, '').trim()
        stdoutBuf = stdoutBuf.slice(idx + 1)
        if (line) process.stdout.write(`[backend] ${line}\n`)
        const r = parseBackendReady(line)
        if (r && !resolved) {
          resolved = true
          clearTimeout(timer)
          resolveP({ base: r.base, token: r.token, proc })
        }
      }
    })

    streamUtf8(proc.stderr, (text) => process.stderr.write(`[backend err] ${text}`))

    proc.once('error', (error) => {
      if (!resolved) {
        resolved = true
        clearTimeout(timer)
        rejectP(new Error(`backend spawn failed: ${error.message}`))
      }
    })

    proc.on('exit', (code) => {
      process.stderr.write(`[backend] exited code=${code}\n`)
      if (!resolved) {
        resolved = true
        clearTimeout(timer)
        rejectP(new Error(`backend exited code=${code}`))
      }
    })
  })

  return { proc, ready }
}
