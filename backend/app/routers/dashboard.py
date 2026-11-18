"""运营看板接口：维护看板视图，覆盖提交视图评审、发布视图、下线视图等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert_attribution import alert_for_dashboard, attribution_service
from app.services.dashboard import DashboardService
from app.store import store

router = APIRouter(prefix="/api/dashboard", tags=["运营看板"])

service = DashboardService()

LIST_FIELDS = ["视图编号", "视图名称", "指标组合", "图表布局", "数据刷新秒", "对比周期", "告警线配置", "订阅接收组"]
STATUSES = ["草稿", "视图评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按视图编号检索"),
    status: str | None = Query(default=None, description="草稿、视图评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按视图编号与状态过滤运营看板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出运营看板清单：返回全量数据（必须注册在 /{entry_id} 之前）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条看板视图，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="看板视图已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条看板视图明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板视图 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/alerts")
def get_alerts(entry_id: int) -> dict[str, Any]:
    """看板恢复观测：返回关联指标告警、告警线状态与归因数据集摘要。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"看板视图 {entry_id} 不存在或已归档")
    alert = alert_for_dashboard(store.find("dashboard", entry_id) or {})
    if alert is None:
        return {"视图编号": entry.get("视图编号"), "items": [], "total": 0}
    dataset = attribution_service.dataset_view(alert)
    return {
        "视图编号": entry.get("视图编号"),
        "items": [{
            "告警编号": alert.get("告警编号"),
            "目标指标": alert.get("目标指标"),
            "指标观测值": alert.get("最新观测值"),
            "告警线": alert.get("告警线"),
            "告警线状态": entry.get("告警线状态"),
            "观测状态": alert.get("观测状态"),
            "当前根因": alert.get("当前根因"),
            "当前根因状态": alert.get("当前根因状态"),
            "数据集编号": alert.get("数据集编号"),
            "数据集状态": dataset.get("状态") if dataset else None,
            "原告警时点": alert.get("原告警时点"),
            "原告警贡献度排名": alert.get("原告警贡献度排名") or [],
        }],
        "total": 1,
    }


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条看板视图执行提交视图评审、发布视图、下线视图；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
