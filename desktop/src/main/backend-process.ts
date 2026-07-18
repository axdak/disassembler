// Backend spawn + UTF-8 stream decoder helper.
//
// Decoupled from index.ts so the spawn env and stream decode are testable
// without bringing up the real backend. In particular, stdout/stderr are
// decoded with `StringDecoder('utf8')` so multibyte sequences (e.g. Chinese
// characters) that straddle Buffer chunk boundaries are reassembled instead
// of being turned into U+FFFD replacement chars (the prior `buf += chunk`
// bug under code page 936).
import { StringDecoder } from 'node:string_decoder'
import { spawn, type ChildProcessByStdio } from 'child_process'
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

/** Spawn the venv backend, decode its stdio, and resolve once BACKEND_READY fires. */
export function startBackendProcess(): Promise<BackendHandle> {
  return new Promise((resolveP, rejectP) => {
    const proc = spawn(VENV_PYTHON, ['-u', BACKEND_MAIN, '--port', '0'], {
      env: buildBackendEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
    })

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

    proc.on('exit', (code) => {
      process.stderr.write(`[backend] exited code=${code}\n`)
      if (!resolved) {
        resolved = true
        clearTimeout(timer)
        rejectP(new Error(`backend exited code=${code}`))
      }
    })
  })
}
