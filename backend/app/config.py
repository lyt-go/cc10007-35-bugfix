"""运行配置：端口、跨域、运行环境与告警归因落库目标。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _default_database_url() -> str:
    """默认落在本机 var/ 目录；联调环境用 DATABASE_URL 覆盖成共享库。"""
    return f"sqlite:///{(BACKEND_DIR / 'var' / 'incident_store.sqlite3').as_posix()}"


def sqlite_path(database_url: str) -> Path:
    """把 sqlite:/// 形式的地址解析成本地路径；非 SQLite 地址直接报错，避免静默降级。"""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"暂不支持的数据库地址：{database_url}")
    raw = database_url[len(prefix):]
    path = Path(raw)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path


@dataclass(frozen=True)
class Settings:
    app_name: str = "智能运营决策分析平台"
    env: str = os.getenv("APP_ENV", "local")
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 告警归因明细数据集的最终落库目标；部署时通过环境变量指向联调环境数据库。
    database_url: str = os.getenv("DATABASE_URL", _default_database_url())
    storage_target: str = os.getenv("STORAGE_TARGET", "联调环境数据库")


settings = Settings()
