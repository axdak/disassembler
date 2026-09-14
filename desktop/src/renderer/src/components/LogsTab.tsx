// LogsTab - 日志
//
// Worker logs 两级分类：一级按流水线阶段，二级按章号/任务名。
// 后端 /logs 解析日志文件名返回 task_type/seq/stage；seq>0 的分章日志按「第N章」细分，
// 其余按 task_type 中文名细分。点击日志名称在共享抽屉中查看内容（App 持有 load + drawer）。
import { useState } from 'react'
import type { CSSProperties } from 'react'
import type { LogEntry } from '../types'

const STAGE_ORDER = [
  '章节分析', '章节Delta', '章节结构', '视觉资产',
  '全书分析', '最终整理', '治理', '其他',
]

// task_type -> 中文标签（二级 tab 用；seq>0 的分章日志直接用「第N章」，不查此表）
const TASK_TYPE_LABEL: Record<string, string> = {
  analysis: '分析', analysis_regenerate: '重生成',
  delta: 'Delta', repair_chapter: '修复',
  chapter_structure: '章节结构',
  visual_assets: '分章', visual_assets_aggregate: '汇总',
  final_draft: '草稿', repair_final: '修复',
  audit_correction: '治理',
  summary: '梗概', characters: '角色', plot: '剧情', style: '文风/节奏',
  worldview: '世界观', settings: '设定', plotlines: '剧情线',
  outline: '大纲', detailed_outline: '详细大纲',
  narrative_structure: '叙事结构', report: '报告',
}

function subKey(l: LogEntry): string {
  if (l.seq > 0) return `第${l.seq}章`
  return TASK_TYPE_LABEL[l.task_type] || l.task_type || '其他'
}

function fmtTime(mtime: number): string {
  const d = new Date(mtime * 1000)
  return d.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function fmtSize(bytes: number): string {
  return bytes < 1024 ? `${bytes}B` : `${(bytes / 1024).toFixed(1)}KB`
}

function tabBtn(active: boolean): CSSProperties {
  return {
    fontSize: 12, fontWeight: active ? 700 : 400, padding: '2px 8px',
    border: '1px solid #ccc', borderRadius: 3, background: active ? '#eef' : '#fff',
  }
}

export function LogsTab({
  logsList,
  onOpenLog,
}: {
  logsList: LogEntry[]
  onOpenLog: (name: string) => void
}) {
  const [stage, setStage] = useState('全部')
  const [sub, setSub] = useState('全部')

  const stages = ['全部', ...STAGE_ORDER.filter((s) => logsList.some((l) => (l.stage || '其他') === s))]
  const stageLogs = stage === '全部' ? logsList : logsList.filter((l) => (l.stage || '其他') === stage)
  const subs = ['全部', ...Array.from(new Set(stageLogs.map(subKey)))]
  const visible = (sub === '全部' ? stageLogs : stageLogs.filter((l) => subKey(l) === sub))
    .slice()
    .sort((a, b) => b.mtime - a.mtime)

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>worker 日志</h3>
      {logsList.length === 0 ? (
        <span style={{ color: '#888' }}>暂无日志</span>
      ) : (
        <>
          {/* 一级：阶段 */}
          <div style={{ marginBottom: 6, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {stages.map((s) => (
              <button key={s} style={tabBtn(stage === s)} onClick={() => { setStage(s); setSub('全部') }}>{s}</button>
            ))}
          </div>
          {/* 二级：章号/任务名（选了具体阶段且子类多于 1 个时才显示） */}
          {stage !== '全部' && subs.length > 2 && (
            <div style={{ marginBottom: 8, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {subs.map((s) => (
                <button key={s} style={tabBtn(sub === s)} onClick={() => setSub(s)}>{s}</button>
              ))}
            </div>
          )}
          <div style={{ marginTop: 4 }}>
            {visible.length === 0 ? (
              <span style={{ color: '#888' }}>无日志</span>
            ) : (
              visible.map((l) => (
                <button
                  key={l.name}
                  onClick={() => onOpenLog(l.name)}
                  title={l.name}
                  style={{ display: 'block', margin: '2px 0', fontSize: 12, textAlign: 'left' }}
                >
                  <span>{l.seq > 0 ? `第${l.seq}章 ` : ''}{l.name}</span>
                  <span style={{ color: '#888' }}> {fmtTime(l.mtime)} {fmtSize(l.size)}</span>
                </button>
              ))
            )}
          </div>
        </>
      )}
      <p style={{ fontSize: 12, color: '#888' }}>点击日志名称在右侧抽屉中查看内容。</p>
    </div>
  )
}
