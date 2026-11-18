"""告警恢复传播与指标归因数据集：看板观测、告警记录、归因结论共用同一套状态。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import persistence
from app.store import store

ALERT_MODULE = "metric_alerts"
EVENT_MODULE = "metric_alert_events"
DATASET_MODULE = "attribution_datasets"
HISTORY_MODULE = "metric_alert_history"
CREDENTIAL_MODULE = "storage_credentials"

DATASET_PREFIX = "ATTRDS"
FINAL_PROCESS_STATUS = "已处理"
PENDING_PROCESS_STATUS = "待处理"


class AttributionError(Exception):
    """可读的业务错误：路由层转成 ok=False，不抛 500。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise AttributionError(f"{field}必须是数字，当前值「{value}」无法识别") from None


def alert_for_dashboard(dashboard: dict[str, Any]) -> dict[str, Any] | None:
    """看板视图 -> 指标告警记录，按视图编号关联，避免用行号猜测。"""
    code = str(dashboard.get("视图编号") or "")
    dashboard_id = int(dashboard.get("id", 0))
    for alert in store.rows(ALERT_MODULE):
        if alert.get("看板视图编号") == code or int(alert.get("dashboard_id", 0)) == dashboard_id:
            return alert
    return None


def alert_for_drill(drill: dict[str, Any]) -> dict[str, Any] | None:
    """归因分析 -> 指标告警记录，按分析编号关联。"""
    code = str(drill.get("分析编号") or "")
    drill_id = int(drill.get("id", 0))
    for alert in store.rows(ALERT_MODULE):
        if alert.get("归因分析编号") == code or int(alert.get("metricdrill_id", 0)) == drill_id:
            return alert
    return None


def dashboard_for_alert(alert: dict[str, Any]) -> dict[str, Any] | None:
    return store.find("dashboard", int(alert.get("dashboard_id", 0)))


def drill_for_alert(alert: dict[str, Any]) -> dict[str, Any] | None:
    return store.find("metricdrill", int(alert.get("metricdrill_id", 0)))


def alert_line_state(alert: dict[str, Any], value: Any = None) -> str:
    """指标最新观测值落在告警线哪一侧；这是“告警线状态”的唯一口径。"""
    raw = alert.get("最新观测值") if value is None else value
    if raw is None:
        return "未知"
    current = _number(raw, "指标观测值")
    line = _number(alert.get("告警线"), "告警线")
    breached = current < line if alert.get("告警方向") == "低于" else current > line
    return "告警线外" if breached else "告警线内"


def root_state(alert: dict[str, Any]) -> str:
    """当前根因只暴露一个状态：已关闭 / 观察中 / 告警中。"""
    state = str(alert.get("当前根因状态") or "")
    if state in {"已关闭", "观察中", "告警中"}:
        return state
    if alert.get("观测状态") in {"已恢复待确认", "已恢复已确认"}:
        return "已恢复待确认"
    return "告警中"


def _alert_events(alert: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in store.rows(EVENT_MODULE)
        if int(event.get("alert_id", 0)) == int(alert.get("id", 0))
    ]


def latest_batch(alert: dict[str, Any]) -> str | None:
    """按事件时点（而不是到达时点）判定最新异常批次，迟到告警不会颠倒口径。"""
    events = [event for event in _alert_events(alert) if event.get("事件时点")]
    if not events:
        return alert.get("最新异常批次") or alert.get("原告警批次")
    newest = max(events, key=lambda event: str(event.get("事件时点")))
    return str(newest.get("批次") or alert.get("最新异常批次") or alert.get("原告警批次"))


def contributions_for_batch(alert: dict[str, Any], batch: str | None = None) -> dict[str, Any]:
    """从异常批次的波动指标事件计算维度贡献度，不读取原告警时点的排名。"""
    batch_code = batch or latest_batch(alert)
    events = [
        event
        for event in _alert_events(alert)
        if event.get("类型") in {"波动事件", "迟到告警"}
        and (not batch_code or event.get("批次") == batch_code)
    ]
    if not events:
        raise AttributionError(f"异常批次「{batch_code or '未指定'}」中没有可用的波动指标事件")

    deltas: list[tuple[dict[str, Any], float]] = []
    for event in events:
        baseline = _number(event.get("基线值"), "基线值")
        observed = _number(event.get("观测值"), "观测值")
        deltas.append((event, observed - baseline))
    total = sum(abs(delta) for _, delta in deltas)
    if total <= 0:
        raise AttributionError(f"异常批次「{batch_code}」的波动量为 0，无法计算维度贡献度")

    deltas.sort(key=lambda item: (-abs(item[1]), str(item[0].get("维度")), str(item[0].get("维度值"))))
    items = []
    for index, (event, delta) in enumerate(deltas, start=1):
        items.append({
            "排名": index,
            "维度": event.get("维度"),
            "维度值": event.get("维度值"),
            "基线值": round(_number(event.get("基线值"), "基线值"), 2),
            "观测值": round(_number(event.get("观测值"), "观测值"), 2),
            "波动量": round(delta, 2),
            "贡献度": round(abs(delta) / total * 100, 2),
            "事件编号": event.get("事件编号"),
            "事件类型": event.get("类型"),
            "事件时点": event.get("事件时点"),
            "批次": event.get("批次"),
        })
    return {"异常批次": batch_code, "事件条数": len(events), "维度贡献度": items}


def format_ranking(items: list[dict[str, Any]]) -> str:
    return "；".join(
        f"{item['排名']}. {item['维度']}：{item['维度值']} {item['贡献度']:.1f}%" for item in items
    )


def _late_alert_events(alert: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in _alert_events(alert) if event.get("类型") == "迟到告警"]


def _resolve_state_bases(alert: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
    """裁决“看板恢复观测”和“迟到告警”两个状态依据。

    规则只依赖数据时点，不依赖请求到达顺序：迟到告警的事件时点早于恢复观测时点即为
    过期依据，被恢复观测覆盖；晚于恢复观测时点则说明恢复后仍有异常，根因进入观察。
    最终只给出一个当前根因状态，另一个依据只写在数据集的状态依据里。
    """
    observed_at = str(alert.get("最新观测时点") or dashboard.get("观测时点") or "")
    inside = alert_line_state(alert) == "告警线内"
    bases: list[dict[str, Any]] = []
    recovery_basis = {
        "依据类型": "看板恢复观测" if inside else "看板告警观测",
        "来源": str(dashboard.get("视图编号") or ""),
        "时点": observed_at,
        "指标值": alert.get("最新观测值"),
        "告警线": alert.get("告警线"),
        "告警线状态": "告警线内" if inside else "告警线外",
        "结论": "采用" if inside else "告警仍成立",
    }
    bases.append(recovery_basis)

    adopted: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    for event in _late_alert_events(alert):
        event_at = str(event.get("事件时点") or "")
        stale = bool(observed_at) and bool(event_at) and event_at < observed_at
        basis = {
            "依据类型": "迟到告警记录",
            "来源": event.get("事件编号"),
            "事件时点": event_at,
            "到达时点": event.get("到达时点"),
            "维度": event.get("维度"),
            "维度值": event.get("维度值"),
            "结论": "被恢复观测覆盖" if stale else "采用",
            "说明": (
                f"事件时点 {event_at} 早于看板恢复观测 {observed_at}，按恢复观测关闭根因"
                if stale
                else f"事件时点 {event_at} 不早于看板恢复观测 {observed_at}，根因保持观察"
            ),
        }
        bases.append(basis)
        if stale:
            covered.append(basis)
        else:
            adopted.append(basis)

    if not inside:
        outcome = "告警中"
    elif adopted:
        outcome = "观察中"
    else:
        outcome = "已关闭"
    return {
        "状态依据": bases,
        "采用迟到告警": adopted,
        "覆盖迟到告警": covered,
        "结论": outcome,
        "最新依据时点": max(
            [observed_at] + [str(event.get("事件时点") or "") for event in _late_alert_events(alert)]
        ),
    }


def _build_dataset(alert: dict[str, Any], drill: dict[str, Any], dashboard: dict[str, Any], final: bool) -> dict[str, Any]:
    contributions = contributions_for_batch(alert)
    resolution = _resolve_state_bases(alert, dashboard)
    # 未关闭前当前根因保持“告警中”，最终关闭或转观察只由 close_attribution 一次性裁决。
    outcome = resolution["结论"] if final else "告警中"
    items = contributions["维度贡献度"]
    top = items[0] if items else {}
    return {
        "数据集类型": "告警归因明细",
        "告警编号": alert.get("告警编号"),
        "看板视图编号": dashboard.get("视图编号"),
        "分析编号": drill.get("分析编号"),
        "目标指标": alert.get("目标指标"),
        "指标编码": alert.get("指标编码"),
        "异常批次": contributions["异常批次"],
        "事件条数": contributions["事件条数"],
        "指标观测值": alert.get("最新观测值"),
        "告警线": alert.get("告警线"),
        "告警线状态": alert_line_state(alert),
        "当前根因": f"{top.get('维度')}：{top.get('维度值')}" if top else None,
        "当前根因状态": outcome,
        "本案根因假设": alert.get("原告警根因"),
        "状态依据": resolution["状态依据"],
        "迟到告警裁决": {
            "采用": resolution["采用迟到告警"],
            "覆盖": resolution["覆盖迟到告警"],
        },
        "维度贡献度": items,
        "贡献度排名": format_ranking(items),
        "原告警时点": alert.get("原告警时点"),
        "原告警贡献度排名": alert.get("原告警贡献度排名") or [],
        "告警轮次": alert.get("告警轮次", 1),
        "状态": outcome,
        "处理状态": FINAL_PROCESS_STATUS if final else PENDING_PROCESS_STATUS,
        "创建时点": now(),
        "确认时点": None,
        "备注": "旧的贡献度排名只保留在历史区，不再作为当前排名展示"
        if final
        else "数据集已保存，等待看板恢复确认与归因关闭",
    }


def _idempotency_key(alert: dict[str, Any]) -> str:
    return f"{alert.get('告警编号')}|{latest_batch(alert)}|{alert.get('告警轮次', 1)}"


def _find_dataset(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    for dataset in store.rows(DATASET_MODULE):
        if dataset.get("数据集编号") == code:
            return dataset
    return None


class AlertAttributionService:
    """看板恢复观测与指标归因关闭的唯一入口。"""

    def __init__(self) -> None:
        self._hydrated = False

    # -- 落库恢复 ---------------------------------------------------------
    def ensure_hydrated(self) -> None:
        """服务重启后把已落库的数据集/凭证/历史读回内存，刷新页面仍能浏览关联明细。"""
        if self._hydrated:
            return
        with store.lock:
            if self._hydrated:
                return
            for dataset in persistence.read_all_datasets():
                if _find_dataset(dataset.get("数据集编号")) is None:
                    store.rows(DATASET_MODULE).append(dataset)
            known_credentials = {row.get("数据集编号") for row in store.rows(CREDENTIAL_MODULE)}
            for credential in persistence.read_all_credentials():
                if credential.get("数据集编号") not in known_credentials:
                    store.rows(CREDENTIAL_MODULE).append(credential)
            known_history = {
                (row.get("数据集编号"), row.get("历史类型")) for row in store.rows(HISTORY_MODULE)
            }
            for row in persistence.read_all_history():
                if (row.get("数据集编号"), row.get("历史类型")) not in known_history:
                    store.rows(HISTORY_MODULE).append(row)
            for alert in store.rows(ALERT_MODULE):
                dataset = _find_dataset(alert.get("数据集编号"))
                if dataset is None:
                    dataset = next(
                        (
                            row
                            for row in store.rows(DATASET_MODULE)
                            if row.get("告警编号") == alert.get("告警编号")
                            and int(row.get("告警轮次", 0)) == int(alert.get("告警轮次", 1))
                        ),
                        None,
                    )
                if dataset is None:
                    continue
                # 重启后状态以落库记录为准：内存里重置过的告警/看板/归因据此恢复成已关闭或观察中。
                alert.update({
                    "数据集编号": dataset.get("数据集编号"),
                    "数据集状态": dataset.get("状态"),
                    "当前根因状态": dataset.get("当前根因状态") or dataset.get("状态"),
                    "观测状态": (
                        "已恢复后再次告警"
                        if dataset.get("状态") == "观察中"
                        else "已恢复已确认"
                    ),
                    "复核状态": "复核中" if dataset.get("状态") == "观察中" else "已复核",
                })
                dashboard = dashboard_for_alert(alert)
                if dashboard is not None:
                    dashboard.update({
                        "当前根因": alert.get("当前根因"),
                        "当前根因状态": alert.get("当前根因状态"),
                        "观测状态": alert.get("观测状态"),
                        "数据集编号": alert.get("数据集编号"),
                    })
                drill = drill_for_alert(alert)
                if drill is not None:
                    drill.update({
                        "status": "分析中" if dataset.get("状态") == "观察中" else "已确认",
                        "pending": dataset.get("状态") == "观察中",
                        "abnormal": dataset.get("状态") == "观察中",
                        "当前根因状态": alert.get("当前根因状态"),
                        "数据集编号": alert.get("数据集编号"),
                        "贡献度排名": dataset.get("贡献度排名") or drill.get("贡献度排名"),
                        "验证结果": (
                            f"看板已回正，根因已关闭（数据集 {dataset.get('数据集编号')}）"
                            if dataset.get("状态") == "已关闭"
                            else f"看板已回正，迟到告警仍在，根因进入观察（数据集 {dataset.get('数据集编号')}）"
                        ),
                    })
            self._hydrated = True

    # -- 数据集保存与关闭 -------------------------------------------------
    def save_dataset(self, alert: dict[str, Any], drill: dict[str, Any]) -> dict[str, Any]:
        self.ensure_hydrated()
        with store.lock:
            existing = _find_dataset(alert.get("数据集编号"))
            dashboard = dashboard_for_alert(alert)
            if dashboard is None:
                raise AttributionError(f"告警 {alert.get('告警编号')} 未关联看板视图，无法保存归因数据集")
            if existing is not None:
                if existing.get("处理状态") == PENDING_PROCESS_STATUS:
                    refreshed = _build_dataset(alert, drill, dashboard, final=False)
                    existing.update(refreshed)
                return existing

            # 同一告警轮次只允许一条有效数据集：并发请求按幂等键复用已存在的那条。
            key = _idempotency_key(alert)
            for row in store.rows(DATASET_MODULE):
                if row.get("幂等键") == key:
                    alert["数据集编号"] = row.get("数据集编号")
                    alert["数据集状态"] = row.get("状态")
                    return row

            rows = store.rows(DATASET_MODULE)
            used = {str(row.get("数据集编号")) for row in rows}
            sequence = len(rows) + 1
            code = f"{DATASET_PREFIX}-{sequence:04d}"
            while code in used:
                sequence += 1
                code = f"{DATASET_PREFIX}-{sequence:04d}"

            dataset = _build_dataset(alert, drill, dashboard, final=False)
            dataset["数据集编号"] = code
            dataset["幂等键"] = _idempotency_key(alert)
            rows.append(dataset)
            alert["数据集编号"] = code
            alert["数据集状态"] = dataset["状态"]
            alert["复核状态"] = "待复核"
            alert["更新时间"] = now()
            return dataset

    def close_attribution(self, alert: dict[str, Any], drill: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
        """看板恢复状态与归因关闭在同一把锁、同一个确认时点完成。"""
        self.ensure_hydrated()
        with store.lock:
            dashboard = dashboard_for_alert(alert)
            if dashboard is None:
                raise AttributionError(f"告警 {alert.get('告警编号')} 未关联看板视图，无法关闭归因")
            if dashboard.get("status") != "已发布":
                raise AttributionError("看板视图尚未发布，不能确认恢复")
            if alert_line_state(alert) != "告警线内":
                raise AttributionError(
                    f"指标最新值 {alert.get('最新观测值')} 仍在告警线 {alert.get('告警线')} 之外，不能关闭根因"
                )

            dataset = _find_dataset(alert.get("数据集编号"))
            if dataset is not None and dataset.get("处理状态") == FINAL_PROCESS_STATUS:
                credential = self._credential_for(str(dataset.get("数据集编号")))
                return dataset, credential, "归因已关闭，返回原告警归因明细数据集与落库凭证"

            dataset = self.save_dataset(alert, drill)

            confirm_time = now()
            final_dataset = _build_dataset(alert, drill, dashboard, final=True)
            final_dataset.update({
                "数据集编号": dataset.get("数据集编号"),
                "幂等键": dataset.get("幂等键"),
                "创建时点": dataset.get("创建时点"),
                "确认时点": confirm_time,
            })
            history_rows = self._history_rows(alert, final_dataset, confirm_time)

            # 先落库，成功后再提交内存状态；失败时不会留下“已关闭但没有凭证”的残留。
            credential = persistence.persist_dataset(final_dataset, history_rows)
            self._remember_credential(credential)

            dataset.update(final_dataset)
            self._store_history(history_rows)
            self._mark_late_events(alert, final_dataset)
            self._commit_state(alert, drill, dashboard, final_dataset, confirm_time, credential)
            return dataset, credential, (
                f"看板恢复与归因关闭已同刻确认，数据集 {dataset['数据集编号']} 已落库"
                if final_dataset["状态"] == "已关闭"
                else f"迟到告警仍在，根因进入观察，数据集 {dataset['数据集编号']} 已落库"
            )

    def refresh_pending(self, alert: dict[str, Any]) -> None:
        """观测变化后刷新未处理数据集，避免数据集与最新观测脱节。"""
        self.ensure_hydrated()
        with store.lock:
            dataset = _find_dataset(alert.get("数据集编号"))
            if dataset is None or dataset.get("处理状态") != PENDING_PROCESS_STATUS:
                return
            drill = drill_for_alert(alert)
            dashboard = dashboard_for_alert(alert)
            if drill is None or dashboard is None:
                return
            dataset.update(_build_dataset(alert, drill, dashboard, final=False))

    # -- 迟到告警补录 -----------------------------------------------------
    def ingest_late_alert(self, alert: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """补录迟到告警；已有最终数据集时只登记复核线索，不同时改写两套根因状态。"""
        self.ensure_hydrated()
        required = ["维度", "维度值", "基线值", "观测值", "事件时点"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            raise AttributionError(f"缺少必填字段：{'、'.join(missing)}")
        baseline = _number(values.get("基线值"), "基线值")
        observed = _number(values.get("观测值"), "观测值")
        with store.lock:
            events = store.rows(EVENT_MODULE)
            event = {
                "id": store.next_id(EVENT_MODULE),
                "事件编号": f"EVT-LATE-{len(events) + 1:04d}",
                "告警编号": alert.get("告警编号"),
                "alert_id": alert.get("id"),
                "批次": values.get("批次") or alert.get("原告警批次"),
                "批次时点": values.get("事件时点"),
                "类型": "迟到告警",
                "目标指标": alert.get("目标指标"),
                "维度": values.get("维度"),
                "维度值": values.get("维度值"),
                "基线值": baseline,
                "观测值": observed,
                "事件时点": values.get("事件时点"),
                "到达时点": values.get("到达时点") or now(),
                "已处理": False,
                "处理结论": None,
            }
            events.append(event)
            dataset = _find_dataset(alert.get("数据集编号"))
            if dataset is not None and dataset.get("处理状态") == FINAL_PROCESS_STATUS:
                observed_at = str(alert.get("最新观测时点") or "")
                stale = bool(observed_at) and str(event["事件时点"]) < observed_at
                if stale:
                    conclusion = f"事件时点 {event['事件时点']} 早于看板恢复观测 {observed_at}，作为过期依据登记"
                    event["已处理"] = True
                    event["处理结论"] = conclusion
                    alert["复核状态"] = "待复核"
                    history_type = f"确认后迟到告警#{event['事件编号']}"
                else:
                    # 恢复之后仍有异常：根因从“已关闭”回到“观察中”，只保留一套当前状态。
                    conclusion = (
                        f"事件时点 {event['事件时点']} 不早于看板恢复观测 {observed_at}，根因重新进入观察"
                    )
                    event["已处理"] = True
                    event["处理结论"] = conclusion
                    self._reopen(alert, dataset, event, conclusion)
                    history_type = f"确认后重开观察#{event['事件编号']}"
                alert["更新时间"] = now()
                history = {
                    "数据集编号": dataset.get("数据集编号"),
                    "告警编号": alert.get("告警编号"),
                    "历史类型": history_type,
                    "事件编号": event["事件编号"],
                    "事件时点": event["事件时点"],
                    "到达时点": event["到达时点"],
                    "结论": conclusion,
                    "记录时点": now(),
                }
                store.rows(HISTORY_MODULE).append({**history, "id": store.next_id(HISTORY_MODULE)})
                self._store_history([history])
                return event, dataset
            self.refresh_pending(alert)
            return event, _find_dataset(alert.get("数据集编号"))

    # -- 展示 -------------------------------------------------------------
    def dataset_view(self, alert: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_hydrated()
        dataset = _find_dataset(alert.get("数据集编号"))
        if dataset is None:
            return None
        drill = drill_for_alert(alert)
        dashboard = dashboard_for_alert(alert)
        view = dict(dataset)
        if drill is not None and dataset.get("处理状态") != FINAL_PROCESS_STATUS:
            contributions = contributions_for_batch(alert)
            view["维度贡献度"] = contributions["维度贡献度"]
            view["贡献度排名"] = format_ranking(contributions["维度贡献度"])
        if dashboard is not None:
            view["看板观测状态"] = dashboard.get("观测状态")
            view["看板当前值"] = dashboard.get("当前值")
        view["告警线状态"] = alert_line_state(alert)
        view["当前根因状态"] = dataset.get("当前根因状态") or dataset.get("状态")
        return view

    def history_view(self, alert: dict[str, Any]) -> list[dict[str, Any]]:
        self.ensure_hydrated()
        code = alert.get("数据集编号")
        rows = [
            dict(row)
            for row in store.rows(HISTORY_MODULE)
            if code and row.get("数据集编号") == code
        ]
        if not rows and alert.get("原告警贡献度排名"):
            rows = [{
                "数据集编号": None,
                "告警编号": alert.get("告警编号"),
                "历史类型": "原告警时点排名",
                "贡献度排名": alert.get("原告警贡献度排名"),
                "根因": alert.get("原告警根因"),
                "时点": alert.get("原告警时点"),
                "记录时点": alert.get("原告警时点"),
                "说明": "旧的贡献度排名只作为历史保留",
            }]
        return rows

    def credential_view(self, dataset: dict[str, Any] | None) -> dict[str, Any] | None:
        if dataset is None:
            return None
        return self._credential_for(str(dataset.get("数据集编号")))

    # -- 内部 -------------------------------------------------------------
    def _remember_credential(self, credential: dict[str, Any]) -> None:
        """凭证按数据集编号去重后登记到内存，列表和详情读到同一张。"""
        with store.lock:
            table = store.rows(CREDENTIAL_MODULE)
            for existing in table:
                if existing.get("数据集编号") == credential.get("数据集编号"):
                    existing.update(credential)
                    return
            table.append(dict(credential))

    def _credential_for(self, dataset_code: str) -> dict[str, Any]:
        with store.lock:
            for credential in store.rows(CREDENTIAL_MODULE):
                if credential.get("数据集编号") == dataset_code:
                    return credential
            persisted = persistence.read_credential(dataset_code)
            if persisted is None:
                raise AttributionError(f"数据集 {dataset_code} 缺少落库凭证，请重新确认落库")
            # 回读和追加在同一把锁里，并发核对不会把同一张凭证写进内存两次。
            for credential in store.rows(CREDENTIAL_MODULE):
                if credential.get("数据集编号") == dataset_code:
                    return credential
            store.rows(CREDENTIAL_MODULE).append(persisted)
            return persisted

    def _history_rows(self, alert: dict[str, Any], dataset: dict[str, Any], confirm_time: str) -> list[dict[str, Any]]:
        rows = [{
            "数据集编号": dataset.get("数据集编号"),
            "告警编号": alert.get("告警编号"),
            "历史类型": "原告警时点排名",
            "原告警时点": alert.get("原告警时点"),
            "贡献度排名": alert.get("原告警贡献度排名") or [],
            "根因": alert.get("原告警根因"),
            "时点": alert.get("原告警时点"),
            "记录时点": confirm_time,
            "说明": "旧的贡献度排名只作为历史保留，不参与当前排名",
        }, {
            "数据集编号": dataset.get("数据集编号"),
            "告警编号": alert.get("告警编号"),
            "历史类型": "看板恢复确认",
            "时点": confirm_time,
            "看板观测值": alert.get("最新观测值"),
            "告警线": alert.get("告警线"),
            "告警线状态": alert_line_state(alert),
            "当前根因状态": dataset.get("状态"),
            "记录时点": confirm_time,
        }]
        for basis in dataset.get("迟到告警裁决", {}).get("覆盖", []):
            rows.append({
                "数据集编号": dataset.get("数据集编号"),
                "告警编号": alert.get("告警编号"),
                "历史类型": f"迟到告警覆盖#{basis.get('来源')}",
                "事件时点": basis.get("事件时点"),
                "到达时点": basis.get("到达时点"),
                "结论": basis.get("说明"),
                "记录时点": confirm_time,
            })
        return rows

    def _store_history(self, history_rows: list[dict[str, Any]]) -> None:
        table = store.rows(HISTORY_MODULE)
        known = {(row.get("数据集编号"), row.get("历史类型")) for row in table}
        for row in history_rows:
            key = (row.get("数据集编号"), row.get("历史类型"))
            if key in known:
                continue
            table.append({**row, "id": store.next_id(HISTORY_MODULE)})
            known.add(key)

    def _mark_late_events(self, alert: dict[str, Any], dataset: dict[str, Any]) -> None:
        verdict = {
            basis.get("来源"): basis
            for basis in dataset.get("状态依据", [])
            if basis.get("依据类型") == "迟到告警记录"
        }
        for event in _late_alert_events(alert):
            basis = verdict.get(event.get("事件编号"))
            if basis is None:
                continue
            event["已处理"] = True
            event["处理结论"] = basis.get("说明")

    def _reopen(
        self,
        alert: dict[str, Any],
        dataset: dict[str, Any],
        event: dict[str, Any],
        conclusion: str,
    ) -> None:
        """恢复后仍出现异常：当前根因切到“观察中”，原告警排名继续留在历史区。"""
        drill = drill_for_alert(alert)
        dashboard = dashboard_for_alert(alert)
        observed_at = str(alert.get("最新观测时点") or "")
        dataset.update({
            "状态": "观察中",
            "当前根因状态": "观察中",
            "最新依据时点": str(event.get("事件时点") or observed_at),
            "确认时点": alert.get("确认时点") or dataset.get("确认时点"),
            "复核说明": conclusion,
        })
        bases = list(dataset.get("状态依据") or [])
        bases.append({
            "依据类型": "迟到告警记录",
            "来源": event.get("事件编号"),
            "事件时点": event.get("事件时点"),
            "到达时点": event.get("到达时点"),
            "维度": event.get("维度"),
            "维度值": event.get("维度值"),
            "结论": "采用",
            "说明": conclusion,
        })
        dataset["状态依据"] = bases
        # 先写库成功，再提交内存状态，避免“根因已重开但没有落库记录”。
        persistence.update_after_reopen(dataset, bases)
        alert.update({
            "当前根因状态": "观察中",
            "观测状态": "已恢复后再次告警",
            "复核状态": "复核中",
        })
        if dashboard is not None:
            dashboard.update({"当前根因状态": "观察中", "观测状态": "已恢复后再次告警"})
        if drill is not None:
            drill.update({
                "status": "分析中",
                "pending": True,
                "abnormal": True,
                "当前根因状态": "观察中",
                "验证结果": conclusion,
            })

    def _commit_state(
        self,
        alert: dict[str, Any],
        drill: dict[str, Any],
        dashboard: dict[str, Any],
        dataset: dict[str, Any],
        confirm_time: str,
        credential: dict[str, Any],
    ) -> None:
        outcome = str(dataset.get("状态"))
        dashboard.update({
            "观测状态": "已恢复已确认",
            "恢复确认时点": confirm_time,
            "当前根因": dataset.get("当前根因"),
            "当前根因状态": outcome,
            "数据集编号": dataset.get("数据集编号"),
            "落库凭证": credential.get("凭证编号"),
        })
        alert.update({
            "观测状态": "已恢复已确认",
            "当前根因": dataset.get("当前根因"),
            "当前根因状态": outcome,
            "数据集编号": dataset.get("数据集编号"),
            "数据集状态": outcome,
            "关闭时点": confirm_time if outcome == "已关闭" else None,
            "确认时点": confirm_time,
            "复核状态": "已复核",
            "落库凭证": credential.get("凭证编号"),
            "更新时间": confirm_time,
        })
        drill.update({
            "status": "已确认" if outcome == "已关闭" else "分析中",
            "pending": outcome != "已关闭",
            "abnormal": outcome != "已关闭",
            "贡献度排名": dataset.get("贡献度排名"),
            "根因假设": dataset.get("当前根因"),
            "验证结果": (
                f"看板已回正，根因已关闭（数据集 {dataset.get('数据集编号')}）"
                if outcome == "已关闭"
                else f"看板已回正，迟到告警仍在，根因进入观察（数据集 {dataset.get('数据集编号')}）"
            ),
            "当前根因状态": outcome,
            "数据集编号": dataset.get("数据集编号"),
            "确认时点": confirm_time,
            "关联看板": dashboard.get("视图编号"),
        })


attribution_service = AlertAttributionService()
