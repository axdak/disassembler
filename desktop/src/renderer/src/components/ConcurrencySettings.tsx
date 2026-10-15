import type { ConcurrencyPolicy } from '../types'

const fields = [
  ['analysis_concurrency', '分析并发量', 1],
  ['per_chapter_concurrency', '分章任务并发量', 2],
] as const

// Conservative client bound when runtime metadata cannot be read. Once loaded,
// the backend supplies the authoritative resource limit; deployment stays unknown.
const fallbackMaximum = 32

/** Keep drafts editable; reject rather than silently round or clamp user input. */
export function concurrencyError(settings: Record<string, unknown>, policy?: ConcurrencyPolicy | null): string {
  const maximum = policy?.max_task_concurrency ?? fallbackMaximum
  for (const [key, label, fallback] of fields) {
    const value = settings[key] === undefined ? fallback : settings[key]
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 ||
      value > maximum) {
      return `${label}必须是 1–${maximum} 的整数。`
    }
  }
  if (settings.chapter_mode !== undefined && !['two_phase', 'interleaved'].includes(String(settings.chapter_mode))) {
    return '请选择有效的章节模式。'
  }
  return ''
}

export function ConcurrencySettings({ settings, onChange, policy }: {
  settings: Record<string, unknown>
  onChange: (settings: Record<string, unknown>) => void
  policy?: ConcurrencyPolicy | null
}) {
  const error = concurrencyError(settings, policy)
  const analysis = Number(settings.analysis_concurrency ?? 1)
  const chapters = Number(settings.per_chapter_concurrency ?? 2)
  const rows: [string, number][] = [['章节分析', analysis], ['章节结构 / 视觉资产', chapters], ['Delta / 周期治理', 1]]
  if (settings.chapter_mode === 'interleaved') rows.push(['交错模式合计', analysis + 1])
  const source = (requested: number) => {
    if (!policy) return '尚未读取'
    if (policy.deployment_limit === null || requested < policy.deployment_limit) return '任务配置'
    if (policy.limit_source === 'invalid_environment') return '部署配置异常，安全降级'
    return requested === policy.deployment_limit ? '任务配置与部署上限' : '部署上限'
  }
  return <section aria-label="运行调度" style={{ border: '1px solid #e0e0e0', borderRadius: 4, padding: '10px 12px', marginTop: 12, background: '#fafafa' }}>
    <h4 style={{ margin: '0 0 6px' }}>运行调度</h4>
    <div style={{ marginTop: 8 }}>
      <label htmlFor="worker_provider">worker provider：</label>
      <select id="worker_provider" value={String(settings.worker_provider || 'agy')}
        onChange={(e) => onChange({ ...settings, worker_provider: e.target.value })}>
        <option value="agy">agy</option><option value="codebuddy">codebuddy</option>
      </select>
    </div>
    <div style={{ marginTop: 8 }}>
      <label htmlFor="chapter_mode">章节模式：</label>
      <select id="chapter_mode" value={String(settings.chapter_mode || 'two_phase')}
        onChange={(e) => onChange({ ...settings, chapter_mode: e.target.value })}>
        <option value="two_phase">两阶段（先全部分析再 delta）</option>
        <option value="interleaved">交错流水线（分析并行+delta 串行重叠）</option>
      </select>
    </div>
    {fields.map(([key, label, fallback]) => <div key={key} style={{ marginTop: 8 }}>
      <label htmlFor={key}>{label}：</label>
      <input id={key} type="number" min={1} max={policy?.max_task_concurrency ?? fallbackMaximum} step={1} style={{ width: 70 }}
        value={String(settings[key] ?? fallback)}
        onChange={(e) => onChange({ ...settings, [key]: e.target.value === '' ? '' : Number(e.target.value) })} />
    </div>)}
    {error && <p role="alert" style={{ color: 'crimson' }}>{error}</p>}
    {policy ? <p style={{ fontSize: 12 }}>
      {policy.deployment_limit === null ? '未设置部署总上限，各任务独立使用自己的额度。' :
        `本进程部署总上限：${policy.deployment_limit}；来源：ND_MAX_CONCURRENT_WORKERS（启动时读取）。`}
    </p> : <p style={{ fontSize: 12 }}>尚未读取部署上限，实际上限未知；请重新打开设置刷新。保存时后端仍会校验。</p>}
    {policy?.warning && <p role="alert" style={{ color: 'crimson' }}>{policy.warning}</p>}
    <table style={{ width: '100%', fontSize: 12, textAlign: 'left', borderSpacing: '0 8px' }}>
      <caption>当前表单预览：每个自动运行任务的阶段上限</caption>
      <thead><tr><th>阶段</th><th>配置并发</th><th>实际生效上限</th><th>限制来源</th></tr></thead>
      <tbody>{rows.map(([label, configured]) => <tr key={label}>
        <th scope="row">{label}</th><td>{error ? '配置无效' : configured}</td>
        <td>{!policy ? '未知' : error ? '配置无效' : Math.min(configured, policy.deployment_limit ?? configured)}</td>
        <td>{label === 'Delta / 周期治理' ? '阶段串行约束' : source(configured)}</td>
      </tr>)}</tbody>
    </table>
    <p style={{ fontSize: 12 }}>上限不是当前活跃数量；待处理章数、依赖、其他任务和模型响应会影响实际并行数量。1 表示逐章处理；并发增大不保证更快。</p>
    <p style={{ fontSize: 12 }}>Delta 和周期治理仍串行，校验与归档顺序不变。交错模式允许分析与一个 Delta 重叠，合计上限为分析并发量加 1，再受部署总上限约束。单项工作台任务内部仍串行。</p>
    <p style={{ fontSize: 12 }}>部署上限仅约束当前后端进程，多个任务共享；其他应用实例和外部 CLI 不在其中。未设置时，多个任务的总并发可能超过表中单任务上限。修改环境变量需重启应用。</p>
    <p style={{ fontSize: 12 }}>保存不会修改正在运行或已经入队的全流程任务；新配置用于后续新任务{policy ? `。并发量允许 1–${policy.max_task_concurrency}，用于限制线程资源，不代表服务商额度。` : '。'}</p>
  </section>
}
