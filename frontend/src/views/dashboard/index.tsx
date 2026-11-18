import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dashboard'
const columns = [
  "视图编号",
  "视图名称",
  "指标组合",
  "图表布局",
  "数据刷新秒",
  "对比周期",
  "告警线配置",
  "订阅接收组"
]
const actions = [
  "提交视图评审",
  "发布视图",
  "下线视图"
]
const statuses = [
  "草稿",
  "视图评审中",
  "已发布",
  "已下线"
]
const stats = [
  {
    "label": "看板视图总数",
    "value": 0
  },
  {
    "label": "已发布视图",
    "value": 0
  },
  {
    "label": "评审中视图",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type RankItem = { 排名: number; 维度: string; 维度值: string; 贡献度: number }

export default function DashboardView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('看板视图列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '运营看板列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '运营看板动作未生效，请稍后重试')
      }
      await reload()
      if (selected && selected.id === row.id) {
        setSelected(payload.entry ?? row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '运营看板操作失败')
    }
  }

  return (
    <section className="page" data-module="dashboard">
      <header className="page-head">
        <div>
          <h2>运营看板管理</h2>
          <p className="page-desc">配置运营数据看板视图，组合多维度指标和实时告警线。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('看板视图登记入口尚未接入审批流')}>
            登记看板视图
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出运营看板清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>当前根因状态</th>
            <th>告警线状态</th>
            <th>关联归因</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selected && selected.id === row.id ? 'row-selected' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row['当前根因状态'] ?? '—'}</td>
              <td>{row['告警线状态'] ?? '—'}</td>
              <td>{row['关联分析编号'] ?? '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void runAction('确认恢复', row)}>
                  确认恢复
                </button>
                <button className="link" type="button" onClick={() => setSelected(row)}>
                  关联明细
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无运营看板数据，可先登记看板视图</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="detail-panel" data-testid="dashboard-detail">
          <header className="detail-head">
            <h3>{String(selected['视图编号'])} · 看板恢复观测与归因</h3>
            <button className="btn ghost" type="button" onClick={() => setSelected(null)}>收起</button>
          </header>
          <div className="detail-grid">
            <span>视图状态</span><strong>{String(selected['status'] ?? '—')}</strong>
            <span>观测状态</span><strong>{String(selected['观测状态'] ?? '—')}</strong>
            <span>当前根因</span><strong>{String(selected['当前根因'] ?? '—')}</strong>
            <span>当前根因状态</span><strong>{String(selected['当前根因状态'] ?? '—')}</strong>
            <span>指标观测值 / 告警线</span>
            <strong>{String(selected['指标观测值'] ?? '—')} / {String(selected['告警线'] ?? '—')}（{String(selected['告警线状态'] ?? '—')}）</strong>
            <span>数据集 / 凭证</span>
            <strong>{String(selected['数据集编号'] ?? '—')} / {String(selected['落库凭证'] ?? '—')}</strong>
            <span>恢复确认时点</span><strong>{String(selected['恢复确认时点'] ?? '—')}</strong>
          </div>
          <h4>历史区（原告警时点排名）</h4>
          <ul className="basis-list">
            {(Array.isArray(selected['原告警贡献度排名']) ? selected['原告警贡献度排名'] as RankItem[] : []).map((rank) => (
              <li key={`${rank.排名}-${rank.维度}`}>{rank.排名}. {rank.维度}：{rank.维度值} {rank.贡献度}%</li>
            ))}
            {(!Array.isArray(selected['原告警贡献度排名']) || (selected['原告警贡献度排名'] as RankItem[]).length === 0) && (
              <li className="muted">暂无原告警时点排名</li>
            )}
          </ul>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条运营看板记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
