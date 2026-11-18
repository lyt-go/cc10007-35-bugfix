"""指标归因接口：状态流转、告警归因明细数据集、历史排名与落库凭证。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert_attribution import (
    alert_for_drill,
    attribution_service,
    dashboard_for_alert,
)
from app.services.metricdrill import MetricdrillService
from app.store import store

router = APIRouter(prefix="/api/metricdrill", tags=["指标归因"])

service = MetricdrillService()

LIST_FIELDS = ["分析编号", "目标指标", "波动方向", "波动幅度", "维度下钻路径", "贡献度排名", "根因假设", "验证结果"]
STATUSES = ["草稿", "分析中", "已确认", "已归档"]


def _alert_or_404(entry_id: int) -> dict[str, Any]:
    entry = store.find("metricdrill", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"归因分析 {entry_id} 不存在或已归档")
    alert = alert_for_drill(entry)
    if alert is None:
        raise HTTPException(status_code=409, detail=f"归因分析 {entry_id} 未关联指标告警记录")
    return alert


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按分析编号检索"),
    status: str | None = Query(default=None, description="草稿、分析中、已确认、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按分析编号与状态过滤指标归因列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出指标归因清单：返回全量数据（必须注册在 /{entry_id} 之前）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metricdrill", "total": total, "items": items}


@router.get("/datasets")
def list_datasets() -> dict[str, Any]:
    """列出告警归因明细数据集；同一告警轮次只应出现一条有效数据集。"""
    attribution_service.ensure_hydrated()
    datasets = [dict(row) for row in store.rows("attribution_datasets")]
    return {"total": len(datasets), "items": datasets}


@router.get("/datasets/{dataset_code}")
def get_dataset(dataset_code: str) -> dict[str, Any]:
    """按数据集编号回读已落库的告警归因明细数据集。"""
    attribution_service.ensure_hydrated()
    for row in store.rows("attribution_datasets"):
        if row.get("数据集编号") == dataset_code:
            return row
    raise HTTPException(status_code=404, detail=f"告警归因明细数据集 {dataset_code} 不存在")


@router.get("/credentials")
def list_credentials() -> dict[str, Any]:
    """列出落库凭证：最终结果留存的核对入口。"""
    attribution_service.ensure_hydrated()
    rows = [dict(row) for row in store.rows("storage_credentials")]
    return {"total": len(rows), "items": rows}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条归因分析，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="归因分析已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条归因分析明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"归因分析 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/history")
def get_history(entry_id: int) -> dict[str, Any]:
    """历史区：保留原告警时点排名，当前排名不从这里取值。"""
    alert = _alert_or_404(entry_id)
    return {
        "分析编号": store.find("metricdrill", entry_id).get("分析编号"),
        "告警编号": alert.get("告警编号"),
        "items": attribution_service.history_view(alert),
    }


@router.get("/{entry_id}/dataset")
def get_entry_dataset(entry_id: int) -> dict[str, Any]:
    """指标归因详情页读取的告警归因明细数据集（含当前维度贡献度与状态依据）。"""
    alert = _alert_or_404(entry_id)
    dataset = attribution_service.dataset_view(alert)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"归因分析 {entry_id} 尚未生成告警归因明细数据集")
    return dataset


@router.get("/{entry_id}/credential")
def get_entry_credential(entry_id: int) -> dict[str, Any]:
    """读取该归因分析的落库凭证。"""
    alert = _alert_or_404(entry_id)
    dataset = attribution_service.dataset_view(alert)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"归因分析 {entry_id} 尚未生成告警归因明细数据集")
    credential = attribution_service.credential_view(dataset)
    if credential is None:
        raise HTTPException(status_code=404, detail=f"数据集 {dataset.get('数据集编号')} 尚未落库")
    return credential


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条归因分析执行发起分析、确认结论、归档分析；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/save-dataset", response_model=ActionResult)
def save_dataset(entry_id: int) -> ActionResult:
    """由指标归因保存告警归因明细数据集并等待处理。"""
    dataset, message = service.save_dataset(entry_id)
    if dataset is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=dataset)


@router.post("/{entry_id}/close", response_model=ActionResult)
def close_attribution(entry_id: int) -> ActionResult:
    """看板恢复状态与归因关闭同刻确认；返回数据集与落库凭证。"""
    entry, message = service.close_attribution(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    alert = alert_for_drill(store.find("metricdrill", entry_id))
    dataset = attribution_service.dataset_view(alert) if alert else None
    credential = attribution_service.credential_view(dataset)
    return ActionResult(
        ok=True,
        message=message,
        entry={"归因分析": entry, "数据集": dataset, "落库凭证": credential},
    )


@router.post("/{entry_id}/late-alerts", response_model=ActionResult)
def ingest_late_alert(entry_id: int, payload: EntryPayload) -> ActionResult:
    """补录迟到告警记录：与看板恢复观测冲突时只保留一个当前根因状态。"""
    alert = _alert_or_404(entry_id)
    try:
        event, dataset = attribution_service.ingest_late_alert(alert, payload.values)
    except Exception as error:
        if hasattr(error, "message"):
            return ActionResult(ok=False, message=str(error.message))
        raise
    return ActionResult(
        ok=True,
        message=f"迟到告警 {event.get('事件编号')} 已登记，当前根因状态仍为「{(dataset or {}).get('当前根因状态')}」",
        entry={"迟到告警": event, "数据集": dataset},
    )


@router.get("/{entry_id}/reconcile")
def reconcile(entry_id: int) -> dict[str, Any]:
    """核对接口：把看板观测、告警记录、归因结论、数据集与凭证放在一张对账单里。"""
    entry = store.find("metricdrill", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"归因分析 {entry_id} 不存在或已归档")
    alert = _alert_or_404(entry_id)
    dashboard = dashboard_for_alert(alert)
    dataset = attribution_service.dataset_view(alert)
    credential = attribution_service.credential_view(dataset)
    return {
        "分析编号": entry.get("分析编号"),
        "看板视图编号": dashboard.get("视图编号") if dashboard else None,
        "看板状态": dashboard.get("status") if dashboard else None,
        "看板观测状态": dashboard.get("观测状态") if dashboard else None,
        "目标指标": alert.get("目标指标"),
        "指标观测值": alert.get("最新观测值"),
        "告警线": alert.get("告警线"),
        "告警线状态": attribution_service_dataset_state(dataset),
        "当前根因": alert.get("当前根因"),
        "当前根因状态": alert.get("当前根因状态"),
        "数据集编号": dataset.get("数据集编号") if dataset else None,
        "数据集处理状态": dataset.get("处理状态") if dataset else None,
        "当前贡献度排名": dataset.get("贡献度排名") if dataset else None,
        "原告警时点排名": alert.get("原告警贡献度排名"),
        "落库凭证": credential,
        "两侧根因一致": (
            (dashboard or {}).get("当前根因") == alert.get("当前根因")
            and (dashboard or {}).get("当前根因状态") == alert.get("当前根因状态")
        ),
    }


def attribution_service_dataset_state(dataset: dict[str, Any] | None) -> str:
    return str((dataset or {}).get("告警线状态") or "未知")
