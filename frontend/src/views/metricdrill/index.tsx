import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/metricdrill'
const columns = [
  "分析编号",
  "目标指标",
  "波动方向",
  "波动幅度",
  "维度下钻路径",
  "贡献度排名",
  "根因假设",
  "验证结果"
]
const actions = [
  "发起分析",
  "确认结论",
  "归档分析"
]
const statuses = [
  "草稿",
  "分析中",
  "已确认",
  "已归档"
]
const stats = [
  {
    "label": "归因分析总数",
    "value": 0
  },
  {
    "label": "已确认分析",
    "value": 0
  },
  {
    "label": "分析中分析",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type RankItem = { 排名: number; 维度: string; 维度值: string; 贡献度: number }
type Basis = { 依据类型: string; 来源?: string; 时点?: string; 事件时点?: string; 到达时点?: string; 结论?: string; 说明?: string }
type Dataset = {
  数据集编号?: string
  异常批次?: string
  事件条数?: number
  指标观测值?: number
  告警线?: number
  告警线状态?: string
  当前根因?: string
  当前根因状态?: string
  状态?: string
  处理状态?: string
  确认时点?: string | null
  维度贡献度?: RankItem[]
  贡献度排名?: string
  状态依据?: Basis[]
}
type HistoryItem = Record<string, string | number | null | RankItem[]>
type Credential = Record<string, string | number | null>
type Detail = {
  dataset: Dataset | null
  history: HistoryItem[]
  credential: Credential | null
  error: string
}

export default function MetricdrillView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('归因分析列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标归因列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadDetail = useCallback(async (row: Row) => {
    setSelected(row)
    setDetail({ dataset: null, history: [], credential: null, error: '' })
    try {
      const [datasetResponse, historyResponse, credentialResponse] = await Promise.all([
        request(`${ENDPOINT}/${row.id}/dataset`),
        request(`${ENDPOINT}/${row.id}/history`),
        request(`${ENDPOINT}/${row.id}/credential`),
      ])
      const historyPayload = historyResponse.ok ? await historyResponse.json() : { items: [] }
      setDetail({
        dataset: datasetResponse.ok ? await datasetResponse.json() : null,
        history: historyPayload.items ?? [],
        credential: credentialResponse.ok ? await credentialResponse.json() : null,
        error: datasetResponse.ok ? '' : '该归因分析尚未生成告警归因明细数据集',
      })
    } catch (error) {
      setDetail({
        dataset: null,
        history: [],
        credential: null,
        error: error instanceof Error ? error.message : '指标归因明细读取失败',
      })
    }
  }, [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '指标归因动作未生效，请稍后重试')
      }
      await reload()
      if (selected && selected.id === row.id) {
        await loadDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标归因操作失败')
    }
  }

  async function closeAttribution(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/close`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '归因关闭未生效')
      }
      await reload()
      await loadDetail(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '归因关闭失败')
    }
  }

  return (
    <section className="page" data-module="metricdrill">
      <header className="page-head">
        <div>
          <h2>指标归因管理</h2>
          <p className="page-desc">对指标异常波动进行多维度归因分析，下钻定位波动根因。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('归因分析登记入口尚未接入审批流')}>
            登记归因分析
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出指标归因清单
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
            <th>数据集</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selected && selected.id === row.id ? 'row-selected' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row['当前根因状态'] ?? '—'}</td>
              <td>{row['告警线状态'] ?? '—'}</td>
              <td>{row['数据集编号'] ?? '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadDetail(row)}>
                  关联明细
                </button>
                <button className="link" type="button" onClick={() => void closeAttribution(row)}>
                  确认恢复关闭
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无指标归因数据，可先登记归因分析</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && detail && (
        <section className="detail-panel" data-testid="metricdrill-detail">
          <header className="detail-head">
            <h3>{String(selected['分析编号'])} · 告警归因明细</h3>
            <button className="btn ghost" type="button" onClick={() => { setSelected(null); setDetail(null) }}>收起</button>
          </header>
          {detail.error && <p className="error-text">{detail.error}</p>}
          {detail.dataset && (
            <>
              <div className="detail-grid">
                <span>数据集编号</span><strong>{detail.dataset.数据集编号 ?? '—'}</strong>
                <span>异常批次</span><strong>{detail.dataset.异常批次 ?? '—'}</strong>
                <span>指标观测值 / 告警线</span>
                <strong>{detail.dataset.指标观测值 ?? '—'} / {detail.dataset.告警线 ?? '—'}（{detail.dataset.告警线状态 ?? '—'}）</strong>
                <span>当前根因</span><strong>{detail.dataset.当前根因 ?? '—'}</strong>
                <span>当前根因状态</span><strong>{detail.dataset.当前根因状态 ?? detail.dataset.状态 ?? '—'}</strong>
                <span>处理状态</span><strong>{detail.dataset.处理状态 ?? '—'}</strong>
                <span>确认时点</span><strong>{detail.dataset.确认时点 ?? '—'}</strong>
              </div>

              <h4>维度贡献度（按异常批次事件计算）</h4>
              <table className="data-table compact">
                <thead><tr><th>排名</th><th>维度</th><th>维度值</th><th>贡献度</th></tr></thead>
                <tbody>
                  {(detail.dataset.维度贡献度 ?? []).map((item) => (
                    <tr key={`${item.排名}-${item.维度}-${item.维度值}`}>
                      <td>{item.排名}</td><td>{item.维度}</td><td>{item.维度值}</td><td>{item.贡献度}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <h4>状态依据</h4>
              <ul className="basis-list">
                {(detail.dataset.状态依据 ?? []).map((basis, index) => (
                  <li key={`${basis.依据类型}-${basis.来源 ?? index}`}>
                    <strong>{basis.依据类型}</strong>：{basis.结论 ?? '—'}
                    {basis.时点 && <span className="muted">（观测 {basis.时点}）</span>}
                    {basis.事件时点 && <span className="muted">（事件 {basis.事件时点} / 到达 {basis.到达时点}）</span>}
                    {basis.说明 && <div className="muted">{basis.说明}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>历史区（原告警时点排名只作历史保留）</h4>
          <table className="data-table compact">
            <thead><tr><th>历史类型</th><th>时点</th><th>内容</th></tr></thead>
            <tbody>
              {detail.history.map((item, index) => (
                <tr key={index}>
                  <td>{String(item['历史类型'] ?? '—')}</td>
                  <td>{String(item['时点'] ?? item['事件时点'] ?? item['记录时点'] ?? '—')}</td>
                  <td>
                    {Array.isArray(item['贡献度排名'])
                      ? (item['贡献度排名'] as RankItem[]).map((rank) => `${rank.排名}. ${rank.维度}：${rank.维度值} ${rank.贡献度}%`).join('；')
                      : String(item['结论'] ?? item['说明'] ?? item['当前根因状态'] ?? '—')}
                  </td>
                </tr>
              ))}
              {detail.history.length === 0 && (
                <tr><td className="empty-state" colSpan={3}>暂无历史记录</td></tr>
              )}
            </tbody>
          </table>

          <h4>落库凭证</h4>
          {detail.credential ? (
            <div className="detail-grid">
              <span>凭证编号</span><strong>{detail.credential['凭证编号']}</strong>
              <span>数据集编号</span><strong>{detail.credential['数据集编号']}</strong>
              <span>落库目标</span><strong>{detail.credential['落库目标']}</strong>
              <span>记录数</span><strong>{detail.credential['记录数']}</strong>
              <span>写入时点</span><strong>{detail.credential['写入时点']}</strong>
              <span>状态</span><strong>{detail.credential['状态']}</strong>
            </div>
          ) : (
            <p className="muted">尚未落库：需先保存数据集并完成看板恢复确认。</p>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条指标归因记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
