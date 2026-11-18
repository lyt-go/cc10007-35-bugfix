"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

# 告警归因用到的支撑表只服务于运营看板 / 指标归因，不作为独立业务模块展示在概览里。
INTERNAL_TABLES = {
    "metric_alerts",
    "metric_alert_events",
    "attribution_datasets",
    "metric_alert_history",
    "storage_credentials",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 状态流转、数据集生成和落库凭证必须是一个事务：所有业务服务共用同一把可重入锁。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def next_id(self, module: str) -> int:
        """在锁内分配自增主键；并发写同一张表时不会拿到重复 id。"""
        rows = self.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
