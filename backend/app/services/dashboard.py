"""运营看板业务规则：状态流转、字段校验与告警恢复传播都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.alert_attribution import (
    alert_for_dashboard,
    alert_line_state,
    attribution_service,
    root_state,
)
from app.store import store

MODULE = "dashboard"
REQUIRED_FIELDS = ["视图编号", "视图名称", "指标组合"]
STATUS_ORDER = ["草稿", "视图评审中", "已发布", "已下线"]
# 每个动作只能从指定状态出发；草稿不能跳过评审直接发布，已发布也不能重复发布。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "提交视图评审": (("草稿",), "视图评审中"),
    "发布视图": (("视图评审中",), "已发布"),
    "确认恢复": (("已发布",), "已发布"),
    "下线视图": (("已发布",), "已下线"),
}
NEGATIVE_ACTIONS = ["下线视图"]


class DashboardService:
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
            rows = [row for row in rows if keyword in str(row.get("视图编号", ""))]
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

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        attribution_service.ensure_hydrated()
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运营看板可执行范围"
        allowed_from, target = ACTION_RULES[action]
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"看板视图 {entry_id} 不存在或已归档"
            current = str(entry.get("status") or "")
            if current not in STATUS_ORDER:
                return None, f"当前状态「{current}」不在允许的状态序列里"
            if current not in allowed_from:
                return None, f"看板视图当前为「{current}」，不能执行「{action}」"

            alert = alert_for_dashboard(entry)
            drill = self._linked_drill(entry)

            if action == "确认恢复":
                # 看板恢复状态与归因关闭在同一把锁、同一个确认时点完成。
                if alert is None or drill is None:
                    return None, "该看板视图未关联指标归因记录，无需确认恢复"
                try:
                    dataset, credential, message = attribution_service.close_attribution(alert, drill)
                except Exception as error:
                    if hasattr(error, "message"):
                        return None, str(error.message)
                    raise
                entry["恢复确认时点"] = dataset.get("确认时点")
                entry["数据集编号"] = dataset.get("数据集编号")
                entry["落库凭证"] = credential.get("凭证编号")
                return self._decorate(entry), message

            if action == "下线视图" and drill is not None:
                if str(drill.get("当前根因状态") or "") in {"告警中", "观察中"}:
                    return None, "关联指标归因仍在告警或观察中，请先完成归因关闭再下线看板"

            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            message = f"看板视图已{action}"

            if alert is not None:
                if action == "发布视图":
                    entry.setdefault("观测状态", alert.get("观测状态", "正常"))
                elif action == "下线视图":
                    entry["观测状态"] = "已下线"
                    alert["观测状态"] = "已下线"
            if drill is not None and alert is not None:
                drill["关联看板"] = entry.get("视图编号")
        return self._decorate(entry), message

    # -- 展示 -------------------------------------------------------------
    def _linked_drill(self, dashboard: dict[str, Any]) -> dict[str, Any] | None:
        alert = alert_for_dashboard(dashboard)
        if alert is None:
            return None
        return store.find("metricdrill", int(alert.get("metricdrill_id", 0)))

    def _decorate(self, entry: dict[str, Any] | None) -> dict[str, Any]:
        if entry is None:
            return {}
        view = dict(entry)
        alert = alert_for_dashboard(entry)
        if alert is None:
            view.setdefault("当前根因状态", None)
            view.setdefault("告警线状态", "未接入告警")
            return view
        drill = store.find("metricdrill", int(alert.get("metricdrill_id", 0)))
        dataset = attribution_service.dataset_view(alert)
        view.update({
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
            "关联分析编号": drill.get("分析编号") if drill else alert.get("归因分析编号"),
            "复核状态": alert.get("复核状态"),
        })
        return view
