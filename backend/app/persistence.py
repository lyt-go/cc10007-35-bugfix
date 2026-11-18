"""告警归因落库：把有效数据集与状态历史写入落库目标，并生成可核对的落库凭证。

写库和内存状态流转在同一把可重入锁里，任何一次并发请求都只会留下一条有效数据集；
重复写入由幂等键在数据库层拦下，凭证也按数据集编号去重。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings, sqlite_path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution_datasets (
    dataset_code TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    alert_code TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    dashboard_code TEXT NOT NULL,
    drill_code TEXT NOT NULL,
    batch_code TEXT NOT NULL,
    status TEXT NOT NULL,
    process_status TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attribution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_code TEXT NOT NULL,
    dimension TEXT NOT NULL,
    dimension_value TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    observed_value REAL NOT NULL,
    delta_value REAL NOT NULL,
    contribution REAL NOT NULL,
    rank_no INTEGER NOT NULL,
    UNIQUE (dataset_code, dimension, dimension_value)
);
CREATE TABLE IF NOT EXISTS attribution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_code TEXT NOT NULL,
    alert_code TEXT NOT NULL,
    history_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (dataset_code, history_type)
);
CREATE TABLE IF NOT EXISTS storage_credentials (
    credential_code TEXT PRIMARY KEY,
    dataset_code TEXT NOT NULL UNIQUE,
    storage_target TEXT NOT NULL,
    database_url TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    written_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    path: Path = sqlite_path(settings.database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _count_records(conn: sqlite3.Connection, dataset_code: str) -> int:
    total = 0
    for table in ("attribution_datasets", "attribution_events", "attribution_history"):
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE dataset_code = ?", (dataset_code,))
        total += int(cursor.fetchone()[0])
    return total


def persist_dataset(dataset: dict[str, Any], history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把数据集、维度贡献度事件和历史排名一次写入目标库，返回落库凭证。

    数据集编号已经存在时直接返回原凭证，不重复写库，保证重复关闭也只对应一条有效数据集。
    """
    dataset_code = str(dataset["数据集编号"])
    payload = dict(dataset)
    payload["维度贡献度"] = dataset.get("维度贡献度", [])
    written_at = _now()
    checksum = _checksum(payload)

    conn = _connect()
    try:
        with conn:
            existing = conn.execute(
                "SELECT credential_code, checksum FROM storage_credentials WHERE dataset_code = ?",
                (dataset_code,),
            ).fetchone()
            if existing:
                current = conn.execute(
                    "SELECT payload FROM storage_credentials WHERE credential_code = ?",
                    (existing[0],),
                ).fetchone()
                return json.loads(current[0])

            conn.execute(
                """INSERT OR IGNORE INTO attribution_datasets
                (dataset_code, idempotency_key, alert_code, metric_code, dashboard_code,
                 drill_code, batch_code, status, process_status, confirmed_at, payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dataset_code,
                    dataset["幂等键"],
                    dataset["告警编号"],
                    dataset["指标编码"],
                    dataset["看板视图编号"],
                    dataset["分析编号"],
                    dataset["异常批次"],
                    dataset["状态"],
                    dataset["处理状态"],
                    dataset["确认时点"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            for item in dataset.get("维度贡献度", []):
                conn.execute(
                    """INSERT OR IGNORE INTO attribution_events
                    (dataset_code, dimension, dimension_value, baseline_value, observed_value,
                     delta_value, contribution, rank_no)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        dataset_code,
                        item["维度"],
                        item["维度值"],
                        float(item["基线值"]),
                        float(item["观测值"]),
                        float(item["波动量"]),
                        float(item["贡献度"]),
                        int(item["排名"]),
                    ),
                )
            for row in history_rows:
                conn.execute(
                    """INSERT OR IGNORE INTO attribution_history
                    (dataset_code, alert_code, history_type, payload, recorded_at)
                    VALUES (?,?,?,?,?)""",
                    (
                        dataset_code,
                        row["告警编号"],
                        row["历史类型"],
                        json.dumps(row, ensure_ascii=False),
                        row["记录时点"],
                    ),
                )

            record_count = _count_records(conn, dataset_code)
            credential_code = f"CRED-{dataset_code.rsplit('-', 1)[-1]}"
            credential = {
                "凭证编号": credential_code,
                "数据集编号": dataset_code,
                "落库目标": settings.storage_target,
                "数据库地址": settings.database_url,
                "记录数": record_count,
                "校验和": checksum,
                "写入时点": written_at,
                "状态": "已落库",
            }
            conn.execute(
                """INSERT OR IGNORE INTO storage_credentials
                (credential_code, dataset_code, storage_target, database_url,
                 record_count, checksum, written_at, payload)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    credential_code,
                    dataset_code,
                    settings.storage_target,
                    settings.database_url,
                    record_count,
                    checksum,
                    written_at,
                    json.dumps(credential, ensure_ascii=False),
                ),
            )
            return credential
    finally:
        conn.close()


def read_credential(dataset_code: str) -> dict[str, Any] | None:
    """按数据集编号回读落库凭证；凭证就是最终结果的留痕。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT payload FROM storage_credentials WHERE dataset_code = ?",
            (dataset_code,),
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def read_persisted_dataset(dataset_code: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT payload FROM attribution_datasets WHERE dataset_code = ?",
            (dataset_code,),
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def update_after_reopen(dataset: dict[str, Any], bases: list[dict[str, Any]]) -> None:
    """根因重开观察时回写数据集；落库凭证不变，校验和按最新数据集内容重算。"""
    dataset_code = str(dataset.get("数据集编号") or "")
    if not dataset_code:
        return
    payload = dict(dataset)
    payload.pop("维度贡献度", None)
    payload["维度贡献度"] = dataset.get("维度贡献度", [])
    checksum = _checksum(payload)
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE attribution_datasets SET status = ?, process_status = ?, payload = ? WHERE dataset_code = ?",
                (
                    dataset.get("状态"),
                    dataset.get("处理状态"),
                    json.dumps(payload, ensure_ascii=False),
                    dataset_code,
                ),
            )
            conn.execute(
                "UPDATE storage_credentials SET checksum = ? WHERE dataset_code = ?",
                (checksum, dataset_code),
            )
            for basis in bases:
                if basis.get("依据类型") != "迟到告警记录":
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO attribution_history
                    (dataset_code, alert_code, history_type, payload, recorded_at)
                    VALUES (?,?,?,?,?)""",
                    (
                        dataset_code,
                        dataset.get("告警编号"),
                        f"确认后迟到告警#{basis.get('来源')}",
                        json.dumps(basis, ensure_ascii=False),
                        _now(),
                    ),
                )
            conn.execute(
                "UPDATE storage_credentials SET record_count = ? WHERE dataset_code = ?",
                (_count_records(conn, dataset_code), dataset_code),
            )
            row = conn.execute(
                "SELECT credential_code, dataset_code, storage_target, database_url, record_count, "
                "checksum, written_at FROM storage_credentials WHERE dataset_code = ?",
                (dataset_code,),
            ).fetchone()
            updated_credential = None
            if row is not None:
                updated_credential = {
                    "凭证编号": row[0],
                    "数据集编号": row[1],
                    "落库目标": row[2],
                    "数据库地址": row[3],
                    "记录数": row[4],
                    "校验和": row[5],
                    "写入时点": row[6],
                    "状态": "已落库",
                }
                conn.execute(
                    "UPDATE storage_credentials SET payload = ? WHERE dataset_code = ?",
                    (json.dumps(updated_credential, ensure_ascii=False), dataset_code),
                )
    finally:
        conn.close()
    if updated_credential is not None:
        from app.store import store

        for credential in store.rows("storage_credentials"):
            if credential.get("数据集编号") == dataset_code:
                credential.update(updated_credential)


def read_all_datasets() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT payload FROM attribution_datasets ORDER BY dataset_code").fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def read_all_credentials() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT payload FROM storage_credentials ORDER BY credential_code").fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def read_all_history() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM attribution_history ORDER BY dataset_code, id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()
