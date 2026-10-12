/** Model Token budgets are independent of character-based packaging thresholds. */
const fields = [
  ['model_input_tokens', '输入上限（Token）', 300000, 1],
  ['model_output_tokens', '单次输出上限（Token）', 64000, 1],
  ['model_context_tokens', '总上下文窗口（Token）', 300000, 0],
  ['model_reserve_tokens', '安全预留（Token）', 30000, 0],
  ['worker_timeout_seconds', '单次调用超时（秒）', 1200, 1],
  ['worker_retries', '单次调用失败重试次数', 2, 0],
] as const

export function modelLimitsState(settings: Record<string, unknown>) {
  const values = Object.fromEntries(fields.map(([key, , fallback]) => [key, Number(settings[key] ?? fallback)]))
  const invalid = fields.find(([key, , , minimum]) => settings[key] === '' ||
    !Number.isSafeInteger(values[key]) || values[key] < minimum)
  const shared = values.model_context_tokens === 0 ? values.model_input_tokens :
    values.model_context_tokens - values.model_output_tokens
  const usable = Math.min(values.model_input_tokens, shared) - values.model_reserve_tokens
  return {
    values, usable,
    error: invalid ? `${invalid[1]}必须是大于或等于 ${invalid[3]} 的整数。` :
      usable <= 0 ? '容量不足：扣除输出和安全预留后，可用输入必须大于零。' : '',
  }
}

export function ModelLimitsSettings({ settings, onChange }: {
  settings: Record<string, unknown>
  onChange: (settings: Record<string, unknown>) => void
}) {
  const { usable, error } = modelLimitsState(settings)
  return <section style={{ border: '1px solid #e0e0e0', borderRadius: 4, padding: '10px 12px', marginTop: 12 }}>
    <h4 style={{ margin: '0 0 8px' }}>模型容量与调用</h4>
    <p style={{ fontSize: 12 }}>适用于章节分析、Delta、修复、周期治理和全书分析与治理。已启动任务和已入队的全流程任务沿用原配置；新设置用于后续新任务。</p>
    {fields.map(([key, label, fallback, minimum]) => <div key={key} style={{ marginTop: 8 }}>
      <label htmlFor={key}>{label}：</label>
      <input id={key} type="number" min={minimum} step={1} style={{ width: 110 }}
        value={String(settings[key] ?? fallback)}
        onChange={(event) => onChange({ ...settings, [key]: event.target.value === '' ? '' : Number(event.target.value) })} />
    </div>)}
    <p role="status"><strong>实际可用输入预算：{error ? '配置无效' : `${usable.toLocaleString('en-US')} Token`}</strong></p>
    {error && <p role="alert" style={{ color: 'crimson' }}>{error}</p>}
    <p style={{ fontSize: 12 }}>计算：min（输入上限，总上下文窗口 − 单次输出上限）− 安全预留。只有服务商明确区分独立输入、输出额度时，才将总上下文窗口设为 0。</p>
    <p style={{ fontSize: 12 }}>Token 与字符不是同一单位；材料用量采用保守估算。该上限约束的是<b>单次模型输出的规模</b>（推理型模型的思维链也计入），必须能容纳最大的单个产物，例如单章 Delta；若某项任务因此被截断，程序会保留断点并提示提高此项，而不是重新分包材料。材料是否超出输入预算由“输入上限”单独判定。外接 CLI 内部追加的历史和工具读取无法精确计量；输出上限能否由服务商强制执行取决于接入能力。</p>
    <p style={{ fontSize: 12 }}>重试次数不含首次调用，0 表示不重试。此项控制单次调用的失败重试；内容修复与换策略另有有界的恢复流程。</p>
  </section>
}
