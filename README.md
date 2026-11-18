# 智能运营决策分析平台

面向用户行为序列分析、RFM分层、流失预警、智能推荐、归因建模、活动效果评估与实时运营决策的数据分析工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为序列 | `sequence` | 序列模式 | 模式编号、用户分群、事件序列 |
| RFM分层 | `rfm` | 分层规则 | 规则编号、最近消费天界、消费频次界 |
| 流失预警 | `churn` | 预警模型 | 模型编号、模型名称、特征变量 |
| 智能推荐 | `recommend` | 推荐策略 | 策略编号、推荐场景、召回算法 |
| 归因建模 | `attrmodel` | 归因模型 | 模型编号、模型名称、归因逻辑 |
| 活动评估 | `activity` | 活动方案 | 方案编号、活动名称、活动类型 |
| 实时决策 | `realtime` | 决策规则 | 规则编号、触发事件、决策条件 |
| 预算分配 | `budget` | 预算方案 | 方案编号、预算总额、渠道分配比例 |
| LTV估算 | `lifetime` | LTV模型 | 模型编号、模型名称、估算方法 |
| 实验变体 | `abvariant` | 变体方案 | 变体编号、关联实验、变体名称 |
| 运营看板 | `dashboard` | 看板视图 | 视图编号、视图名称、指标组合 |
| 评分卡 | `scorecard` | 评分卡 | 评分编号、评分对象、特征变量组 |
| 聚类分群 | `cluster` | 聚类任务 | 任务编号、聚类算法、特征维度 |
| 对比分析 | `comparison` | 对比组 | 对比编号、对比名称、实验组筛选 |
| 数据漂移 | `datadrift` | 漂移检测 | 检测编号、监控数据集、基线版本 |
| 推送计划 | `pushplan` | 推送方案 | 方案编号、推送内容、目标人群 |
| 指标归因 | `metricdrill` | 归因分析 | 分析编号、目标指标、波动方向 |
| 策略库 | `tacticlib` | 运营策略 | 策略编号、策略名称、策略类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 告警恢复与归因数据集

运营看板与指标归因通过一份指标告警记录（`metric_alerts`）共享状态，看板视图编号 ↔
分析编号 ↔ 告警编号三者按数据字段关联，不靠行号猜测。

- **状态机**：看板只能 `草稿 → 视图评审中 → 已发布 → 已下线`，归因只能
  `草稿 → 分析中 → 已确认 → 已归档`；草稿不能跳过评审发布，也不能跳过分析直接确认。
- **恢复传播**：指标观测值回到告警线内后，看板的“确认恢复”与归因关闭在同一个锁、
  同一个确认时点完成；看板与归因两侧的当前根因状态始终一致。
- **贡献度口径**：归因数据集从**最新异常批次**的波动指标事件重新计算维度贡献度，
  原告警时点的排名只写入历史区（`/api/metricdrill/{id}/history`），不再作为当前排名。
- **迟到告警裁决**：看板恢复观测与迟到告警落到同一指标时，按事件时点而不是到达顺序
  裁决——事件时点早于恢复观测即为过期依据被覆盖；晚于恢复观测则根因从已关闭转
  “观察中”。任何时刻只暴露一个当前根因状态。
- **落库凭证**：关闭时先写 SQLite（数据集、维度事件、历史排名）再提交内存状态，
  并发/重复关闭按幂等键只保留一条有效数据集；凭证可按
  `/api/metricdrill/{id}/credential` 回读，服务重启后自动从库里恢复。

相关接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/dashboard/{id}/actions` `{"action":"确认恢复"}` | 看板恢复确认（触发归因同刻关闭） |
| `POST /api/metricdrill/{id}/save-dataset` | 指标归因保存数据集并等待处理 |
| `POST /api/metricdrill/{id}/close` | 关闭归因，返回数据集与落库凭证 |
| `POST /api/metricdrill/{id}/late-alerts` | 补录迟到告警记录 |
| `GET /api/metricdrill/{id}/dataset` | 告警归因明细数据集（含维度贡献度、状态依据） |
| `GET /api/metricdrill/{id}/history` | 历史区（原告警时点排名等） |
| `GET /api/metricdrill/{id}/credential` | 落库凭证 |
| `GET /api/metricdrill/{id}/reconcile` | 看板/告警/归因/凭证对账单 |
| `GET /api/dashboard/{id}/alerts` | 看板恢复观测与告警线状态 |

落库目标由 `DATABASE_URL` / `STORAGE_TARGET` 配置，默认写到 `backend/var/incident_store.sqlite3`；
联调环境把 `DATABASE_URL` 指向共享库即可，业务代码不用改。

回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

领域：数据工程
子方向：数据分析与数据应用
