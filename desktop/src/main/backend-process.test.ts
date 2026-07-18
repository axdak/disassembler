import { describe, it, expect } from 'vitest'
import { Readable } from 'node:stream'
import { buildBackendEnv, parseBackendReady, streamUtf8 } from './backend-process'

/** Collect all emitted text from a streamUtf8 subscription into a string. */
function collectFromChunks(chunks: Buffer[]): Promise<string> {
  return new Promise((resolve) => {
    const stream = new Readable({ read() { /* noop */ } })
    let out = ''
    const dispose = streamUtf8(stream, (text) => { out += text })
    chunks.forEach((c) => stream.push(c))
    stream.push(null)
    stream.on('end', () => {
      dispose()
      resolve(out)
    })
  })
}

describe('buildBackendEnv', () => {
  it('sets PYTHONUTF8=1 and PYTHONIOENCODING=utf-8', () => {
    const env = buildBackendEnv()
    expect(env.PYTHONUTF8).toBe('1')
    expect(env.PYTHONIOENCODING).toBe('utf-8')
  })

  it('preserves proxy and bytecode env vars', () => {
    const env = buildBackendEnv()
    expect(env.NO_PROXY).toBe('127.0.0.1,localhost')
    expect(env.PYTHONDONTWRITEBYTECODE).toBe('1')
  })
})

describe('parseBackendReady', () => {
  it('parses a BACKEND_READY line into base + token', () => {
    const r = parseBackendReady('BACKEND_READY host=127.0.0.1 port=4321 token=abc-123')
    expect(r).toEqual({ base: 'http://127.0.0.1:4321', token: 'abc-123' })
  })

  it('returns null for non-ready lines', () => {
    expect(parseBackendReady('INFO:     Uvicorn running on http://127.0.0.1:4321')).toBeNull()
  })
})

describe('streamUtf8 multibyte reassembly', () => {
  // "小说解析器" — six Chinese chars, each 3 bytes in UTF-8.
  const text = '小说解析器'

  it('decodes the full string intact when not split', async () => {
    const out = await collectFromChunks([Buffer.from(text, 'utf8')])
    expect(out).toBe(text)
    expect(out).not.toContain('�')
  })

  it('reassembles a multibyte sequence split at a byte boundary (no U+FFFD)', async () => {
    const bytes = Buffer.from(text, 'utf8')
    // Split at byte 1 — first char (0xE5 0xB0 0x8F) is split as [0xE5] | [0xB0 0x8F ...].
    const a = bytes.subarray(0, 1)
    const b = bytes.subarray(1)
    const out = await collectFromChunks([a, b])
    expect(out).toBe(text)
    expect(out).not.toContain('�')
  })

  it('reassembles when every multibyte first byte lands alone in its own chunk', async () => {
    const bytes = Buffer.from(text, 'utf8')
    // Feed odd 1-byte chunks then the rest — emulates adversarial chunking.
    const chunks: Buffer[] = []
    for (let i = 0; i < bytes.length; i += 2) {
      chunks.push(bytes.subarray(i, i + 2))
    }
    const out = await collectFromChunks(chunks)
    expect(out).toBe(text)
    expect(out).not.toContain('�')
  })

  it('decodes mixed Chinese + ASCII log content across a mid-char split', async () => {
    const payload = '[backend] 开始处理 第1章\n'
    const bytes = Buffer.from(payload, 'utf8')
    // Split inside "开" (0xE5 0xBC 0x80): chunks land at byte 12 and 13.
    const a = bytes.subarray(0, 12)
    const b = bytes.subarray(12)
    const out = await collectFromChunks([a, b])
    expect(out).toBe(payload)
    expect(out).not.toContain('�')
  })
})
