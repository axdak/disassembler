// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsView } from '../App'
import type { ConcurrencyPolicy } from '../types'

afterEach(cleanup)

const policy: ConcurrencyPolicy = { deployment_limit: 2, limit_source: 'environment',
  warning: '', scope: 'backend_process', max_task_concurrency: 32 }

function Editor({ cap = policy, save = vi.fn() }: { cap?: ConcurrencyPolicy | null; save?: () => void }) {
  const [settings, setSettings] = useState<Record<string, unknown>>({
    analysis_concurrency: 5, per_chapter_concurrency: 10, chapter_mode: 'two_phase',
  })
  return <SettingsView settings={settings} setSettings={setSettings} onSave={save} concurrencyPolicy={cap} />
}

describe('运行并发设置', () => {
  it('显示配置值、部署限制和各阶段实际上限，表单修改即时预览', () => {
    render(<Editor />)
    const analysis = screen.getByRole('row', { name: /章节分析/ })
    expect(within(analysis).getByText('5')).toBeInTheDocument()
    expect(within(analysis).getByText('2')).toBeInTheDocument()
    expect(within(analysis).getByText(/部署上限/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('分析并发量：'), { target: { value: '1' } })
    expect(within(analysis).getByText('任务配置')).toBeInTheDocument()
    expect(screen.getByText(/仅约束当前后端进程/)).toBeInTheDocument()
    expect(screen.getByText(/不会修改正在运行/)).toBeInTheDocument()
  })

  it('交错模式明确计算分析加一个 Delta，总量仍受部署上限约束', () => {
    render(<Editor />)
    fireEvent.change(screen.getByLabelText('章节模式：'), { target: { value: 'interleaved' } })
    const combined = screen.getByRole('row', { name: /交错模式合计/ })
    expect(within(combined).getByText('6')).toBeInTheDocument()
    expect(within(combined).getByText('2')).toBeInTheDocument()
    expect(screen.getByText(/Delta 和周期治理仍串行/)).toBeInTheDocument()
  })

  it('拒绝小数、空值和越界输入，保留用户输入便于改正', () => {
    const save = vi.fn()
    render(<Editor save={save} />)
    const input = screen.getByLabelText('分章任务并发量：')
    for (const value of ['1.5', '', '0', '33']) {
      fireEvent.change(input, { target: { value } })
      expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
      expect(screen.getByRole('alert')).toHaveTextContent('整数')
    }
    fireEvent.change(input, { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(save).toHaveBeenCalledOnce()
  })

  it('读取失败时不把未知上限显示成无限制', () => {
    render(<Editor cap={null} />)
    expect(screen.getByText(/尚未读取部署上限/)).toBeInTheDocument()
    expect(screen.getAllByText('未知')).toHaveLength(3)
    expect(screen.queryByText(/未设置部署总上限/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('分析并发量：'), { target: { value: '33' } })
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
  })

  it('没有部署总上限时使用任务额度，并标明串行阶段的限制来源', () => {
    render(<Editor cap={{ ...policy, deployment_limit: null, limit_source: 'task_config' }} />)
    const row = screen.getByRole('row', { name: /Delta \/ 周期治理/ })
    expect(within(row).getByText('阶段串行约束')).toBeInTheDocument()
    expect(screen.getByText(/未设置部署总上限/)).toBeInTheDocument()
  })

  it('部署配置异常时说明安全降级原因', () => {
    render(<Editor cap={{ ...policy, deployment_limit: 1, limit_source: 'invalid_environment',
      warning: '部署配置无效，已安全降为 1' }} />)
    expect(screen.getByText('部署配置无效，已安全降为 1')).toBeInTheDocument()
  })
})
