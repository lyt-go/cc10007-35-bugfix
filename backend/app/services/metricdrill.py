"""指标归因业务规则：状态流转、告警归因明细数据集与落库凭证都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.alert_attribution import (
    alert_for_drill,
    alert_line_state,
    attribution_service,
    dashboard_for_alert,
    root_state,
)
from app.store import store

MODULE = "metricdrill"
REQUIRED_FIELDS = ["分析编号", "目标指标", "波动方向"]
STATUS_ORDER = ["草稿", "分析中", "已确认", "已归档"]
# 必须按 草稿 -> 分析中 -> 已确认 -> 已归档 逐级流转，不能跳过分析直接确认。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "发起分析": (("草稿",), "分析中"),
    "确认结论": (("分析中",), "已确认"),
    "归档分析": (("已确认",), "已归档"),
}
NEGATIVE_ACTIONS: list[str] = []


class MetricdrillService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        attribution_service.ensure_hydrated()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分析编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        attribution_service.ensure_hydrated()
        entry = store.find(MODULE, entry_id)
        return self._decorate(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行状态动作；确认结论即“由指标归因保存数据集并等待处理”。"""
        attribution_service.ensure_hydrated()
        values = values or {}
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标归因可执行范围"
        allowed_from, target = ACTION_RULES[action]
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"归因分析 {entry_id} 不存在或已归档"
            current = str(entry.get("status") or "")
            if current not in STATUS_ORDER:
                return None, f"当前状态「{current}」不在允许的状态序列里"
            if current not in allowed_from:
                if target == "已确认" and current == "已确认":
                    return None, "该归因分析已确认，不能重复确认"
                return None, f"归因分析当前为「{current}」，不能执行「{action}」"

            if action == "确认结论":
                alert = alert_for_drill(entry)
                if alert is None:
                    return None, "该归因分析未关联指标告警记录，无法保存告警归因明细数据集"
                existing = attribution_service.dataset_view(alert)
                if existing is not None and entry.get("数据集编号"):
                    if existing.get("处理状态") == "待处理":
                        return None, (
                            f"告警归因明细数据集 {existing.get('数据集编号')} 已保存并等待处理，不能重复确认"
                        )
                    return None, f"该归因分析已确认并落库（数据集 {existing.get('数据集编号')}），不能重复确认"
                try:
                    dataset = attribution_service.save_dataset(alert, entry)
                except Exception as error:  # 业务错误原样返回，未预期的错误也不留下半截状态
                    if hasattr(error, "message"):
                        return None, str(error.message)
                    raise
                entry["status"] = "分析中"
                entry["pending"] = True
                entry["abnormal"] = True
                entry["贡献度排名"] = dataset.get("贡献度排名")
                entry["根因假设"] = dataset.get("当前根因")
                entry["验证结果"] = "告警归因明细数据集已保存，等待看板恢复确认与归因关闭"
                entry["数据集编号"] = dataset.get("数据集编号")
                entry["当前根因状态"] = dataset.get("状态")
                return self._decorate(entry), (
                    f"归因分析已确认，告警归因明细数据集 {dataset.get('数据集编号')} 已保存并等待处理"
                )

            if action == "归档分析":
                alert = alert_for_drill(entry)
                if alert is not None:
                    dataset = attribution_service.dataset_view(alert)
                    if dataset is not None and dataset.get("处理状态") != "已处理":
                        return None, "告警归因明细数据集仍在等待处理，不能归档"

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._decorate(entry), f"归因分析已{action}"

    def save_dataset(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """由指标归因保存告警归因明细数据集并等待处理。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归因分析 {entry_id} 不存在或已归档"
        alert = alert_for_drill(entry)
        if alert is None:
            return None, "该归因分析未关联指标告警记录，无法保存告警归因明细数据集"
        try:
            dataset = attribution_service.save_dataset(alert, entry)
        except Exception as error:
            if hasattr(error, "message"):
                return None, str(error.message)
            raise
        return dataset, f"告警归因明细数据集 {dataset.get('数据集编号')} 已保存，等待处理"

    def close_attribution(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """看板恢复状态与归因关闭同刻确认，并返回落库凭证。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归因分析 {entry_id} 不存在或已归档"
        alert = alert_for_drill(entry)
        if alert is None:
            return None, "该归因分析未关联指标告警记录，无法关闭归因"
        try:
            dataset, credential, message = attribution_service.close_attribution(alert, entry)
        except Exception as error:
            if hasattr(error, "message"):
                return None, str(error.message)
            raise
        entry["落库凭证"] = credential.get("凭证编号")
        return self._decorate(entry), message

    # -- 展示 -------------------------------------------------------------
    def _decorate(self, entry: dict[str, Any] | None) -> dict[str, Any]:
        if entry is None:
            return {}
        view = dict(entry)
        alert = alert_for_drill(entry)
        if alert is None:
            view.setdefault("当前根因状态", None)
            view.setdefault("告警线状态", "未接入告警")
            return view
        dashboard = dashboard_for_alert(alert)
        dataset = attribution_service.dataset_view(alert)
        view.update({
            "关联看板": dashboard.get("视图编号") if dashboard else alert.get("看板视图编号"),
            "当前根因": alert.get("当前根因"),
            "当前根因状态": root_state(alert),
            "告警线状态": alert_line_state(alert),
            "指标观测值": alert.get("最新观测值"),
            "告警线": alert.get("告警线"),
            "观测时点": alert.get("最新观测时点"),
            "数据集编号": alert.get("数据集编号"),
            "数据集状态": dataset.get("状态") if dataset else None,
            "原告警时点": alert.get("原告警时点"),
            "原告警贡献度排名": alert.get("原告警贡献度排名") or [],
            "复核状态": alert.get("复核状态"),
        })
        return view
