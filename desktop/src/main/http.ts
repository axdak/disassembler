/** Parse one backend response and turn non-2xx results into rejected IPC calls. */
export async function parseBackendResponse(response: Response): Promise<any> {
  const text = await response.text()
  let payload: any = null

  if (text) {
    try {
      payload = JSON.parse(text)
    } catch (error) {
      if (response.ok) {
        throw new Error(`后端返回了无效 JSON（HTTP ${response.status}）`, { cause: error })
      }
      payload = text
    }
  }

  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload
      ? payload.detail
      : payload
    const message = typeof detail === 'string'
      ? detail
      : detail == null
        ? response.statusText || '未知错误'
        : JSON.stringify(detail)
    throw new Error(`后端请求失败（HTTP ${response.status}）：${message}`)
  }

  return payload
}
