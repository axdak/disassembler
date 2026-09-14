import { describe, expect, it } from 'vitest'
import { parseBackendResponse } from './http'

describe('parseBackendResponse', () => {
  it('returns parsed JSON for successful responses', async () => {
    const response = new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })

    await expect(parseBackendResponse(response)).resolves.toEqual({ ok: true })
  })

  it('throws the backend detail for non-2xx responses', async () => {
    const response = new Response(JSON.stringify({ detail: '项目已在运行' }), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    })

    await expect(parseBackendResponse(response)).rejects.toThrow(
      '后端请求失败（HTTP 409）：项目已在运行',
    )
  })

  it('reports invalid JSON from a successful backend response', async () => {
    const response = new Response('not-json', { status: 200 })

    await expect(parseBackendResponse(response)).rejects.toThrow('后端返回了无效 JSON')
  })
})
