/** Coalesce bursts and serialize reads, retaining one trailing refresh if needed. */
export function eventRefresh(read: () => Promise<unknown>, delay = 200) {
  let stopped = false
  let running = false
  let dirty = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const flush = async () => {
    timer = undefined
    if (stopped || running || !dirty) return
    dirty = false
    running = true
    try { await read() } catch { /* The owning view presents request errors. */ }
    finally {
      running = false
      if (dirty && !stopped) timer = setTimeout(flush, delay)
    }
  }
  return {
    request() {
      if (stopped) return
      dirty = true
      if (!running && timer === undefined) timer = setTimeout(flush, delay)
    },
    dispose() {
      stopped = true
      if (timer !== undefined) clearTimeout(timer)
    },
  }
}
