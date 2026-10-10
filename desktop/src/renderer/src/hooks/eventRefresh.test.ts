import { afterEach, expect, it, vi } from 'vitest'
import { eventRefresh } from './eventRefresh'

afterEach(() => vi.useRealTimers())

it('coalesces bursts and retains one serialized trailing refresh', async () => {
  vi.useFakeTimers()
  let complete: () => void = () => {}
  const read = vi.fn(() => new Promise<void>((resolve) => { complete = resolve }))
  const refresh = eventRefresh(read)
  for (let i = 0; i < 100; i++) refresh.request()
  await vi.advanceTimersByTimeAsync(250)
  expect(read).toHaveBeenCalledTimes(1)
  for (let i = 0; i < 100; i++) refresh.request()
  await vi.advanceTimersByTimeAsync(3000)
  expect(read).toHaveBeenCalledTimes(1)
  complete()
  await vi.advanceTimersByTimeAsync(250)
  expect(read).toHaveBeenCalledTimes(2)
  refresh.request()
  refresh.dispose()
  complete()
  await vi.advanceTimersByTimeAsync(5000)
  expect(read).toHaveBeenCalledTimes(2)
})

it('does not turn event refresh into periodic polling', async () => {
  vi.useFakeTimers()
  const read = vi.fn(async () => {})
  const refresh = eventRefresh(read)
  refresh.request()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(read).toHaveBeenCalledTimes(1)
  refresh.dispose()
})
