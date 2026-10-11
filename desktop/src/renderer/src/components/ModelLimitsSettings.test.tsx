// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsView } from '../App'

afterEach(cleanup)

function Editor({ save }: { save: () => void }) {
  const [settings, setSettings] = useState<Record<string, unknown>>({})
  return <SettingsView settings={settings} setSettings={setSettings} onSave={save} />
}

describe('模型容量设置', () => {
  it('显示有效 Token 预算，并阻止保存无效组合', () => {
    const save = vi.fn()
    render(<Editor save={save} />)
    expect(screen.getByRole('status')).toHaveTextContent('250,000 Token')
    fireEvent.change(screen.getByLabelText('总上下文窗口（Token）：'), { target: { value: '40000' } })
    expect(screen.getByRole('alert')).toHaveTextContent('容量不足')
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(save).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('总上下文窗口（Token）：'), { target: { value: '100000' } })
    expect(screen.getByRole('status')).toHaveTextContent('50,000 Token')
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(save).toHaveBeenCalledOnce()
  })

  it('保留零重试与独立额度，允许输入途中留空但不能保存', () => {
    render(<Editor save={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('总上下文窗口（Token）：'), { target: { value: '0' } })
    expect(screen.getByRole('status')).toHaveTextContent('270,000 Token')
    fireEvent.change(screen.getByLabelText('单次调用失败重试次数：'), { target: { value: '0' } })
    expect(screen.getByRole('button', { name: '保存' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('输入上限（Token）：'), { target: { value: '' } })
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
    expect(screen.getByText('任务包字符预算:')).toBeInTheDocument()
  })
})
