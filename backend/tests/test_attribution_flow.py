"""告警恢复传播与归因数据集回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
测试通过 HTTP 打真实服务进程（内存状态 + SQLite 落库都走真实链路）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerFixture:
    def __init__(self) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.db_dir = BACKEND_DIR / "var" / f"test-{self.port}"
        self.database_url = f"sqlite:///{(self.db_dir / 'incident.sqlite3').as_posix()}"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        env = os.environ | {
            "DATABASE_URL": self.database_url,
            "STORAGE_TARGET": "测试联调库",
        }
        self.process = subprocess.Popen(
            [
                str(BACKEND_DIR / ".venv" / "bin" / "uvicorn"),
                "app.main:app",
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--log-level", "warning",
            ],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base}/api/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("后端未能在 20 秒内启动")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            if self.process.stderr is not None:
                self.process.stderr.close()
            self.process = None

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())


class AttributionFlowTest(unittest.TestCase):
    """每个用例都在独立的服务进程与独立 SQLite 库上跑，互不污染。"""

    def setUp(self) -> None:
        self.server = ServerFixture()
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        for path in sorted(self.server.db_dir.glob("*"), reverse=True):
            path.unlink(missing_ok=True)
        self.server.db_dir.rmdir()

    # -- 工具 -------------------------------------------------------------
    def act(self, module: str, entry_id: int, action: str) -> tuple[int, dict]:
        return self.server.request(
            f"/api/{module}/{entry_id}/actions", "POST", {"values": {"action": action}}
        )

    # -- 复现 1：草稿直接发布 ---------------------------------------------
    def test_draft_cannot_be_published_directly(self) -> None:
        status, payload = self.act("dashboard", 1, "发布视图")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("草稿", payload["message"])
        _, entry = self.server.request("/api/dashboard/1")
        self.assertEqual(entry["status"], "草稿")

        # 正常路径仍然可用：草稿 -> 评审 -> 发布
        self.assertTrue(self.act("dashboard", 1, "提交视图评审")[1]["ok"])
        self.assertTrue(self.act("dashboard", 1, "发布视图")[1]["ok"])
        # 重复发布被拦下
        self.assertFalse(self.act("dashboard", 1, "发布视图")[1]["ok"])

    # -- 复现 2：跳过分析直接确认 -----------------------------------------
    def test_draft_cannot_confirm_without_analysis(self) -> None:
        status, payload = self.act("metricdrill", 1, "确认结论")
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        _, entry = self.server.request("/api/metricdrill/1")
        self.assertEqual(entry["status"], "草稿")

        self.assertTrue(self.act("metricdrill", 1, "发起分析")[1]["ok"])
        # 未关联指标告警记录的归因分析不能凭空确认
        self.assertFalse(self.act("metricdrill", 1, "确认结论")[1]["ok"])
        # 已关联的分析仍不能从“分析中”直接归档，必须先确认结论
        self.assertFalse(self.act("metricdrill", 2, "归档分析")[1]["ok"])
        self.assertTrue(self.act("metricdrill", 2, "确认结论")[1]["ok"])
        # 重复确认被拦下
        self.assertFalse(self.act("metricdrill", 2, "确认结论")[1]["ok"])

    # -- 复现 3 + 4：恢复传播、贡献度按异常批次重算 -----------------------
    def test_recovery_propagates_and_ranking_comes_from_latest_batch(self) -> None:
        status, payload = self.server.request("/api/metricdrill/2/close", "POST", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        dataset = payload["entry"]["数据集"]
        credential = payload["entry"]["落库凭证"]

        # 贡献度按最新异常批次 BATCH-20261117-01 的事件重算，不再沿用原告警排名
        self.assertEqual(dataset["异常批次"], "BATCH-20261117-01")
        self.assertEqual(dataset["状态"], "已关闭")
        self.assertEqual(dataset["处理状态"], "已处理")
        top = dataset["维度贡献度"][0]
        self.assertEqual((top["维度"], top["维度值"], top["贡献度"]), ("渠道", "自然流量", 45.0))
        contributions = [item["贡献度"] for item in dataset["维度贡献度"]]
        self.assertAlmostEqual(sum(contributions), 100.0, places=2)

        # 原告警排名只保留在历史区
        self.assertEqual(dataset["原告警贡献度排名"][0]["维度值"], "自然流量")
        self.assertEqual(dataset["原告警贡献度排名"][1]["维度值"], "华东")
        self.assertNotIn("华东", dataset["贡献度排名"])

        # 看板与归因两侧同刻确认，状态一致
        _, board = self.server.request("/api/dashboard/3")
        _, drill = self.server.request("/api/metricdrill/2")
        self.assertEqual(board["当前根因状态"], "已关闭")
        self.assertEqual(drill["当前根因状态"], "已关闭")
        self.assertEqual(board["恢复确认时点"], drill["确认时点"])
        self.assertEqual(board["数据集编号"], drill["数据集编号"])
        self.assertEqual(board["告警线状态"], "告警线内")

        # 落库凭证可回读
        _, reread = self.server.request("/api/metricdrill/2/credential")
        self.assertEqual(reread["凭证编号"], credential["凭证编号"])
        self.assertEqual(reread["数据集编号"], dataset["数据集编号"])
        self.assertGreater(reread["记录数"], 0)

    # -- 迟到告警与恢复观测同时落到同一指标 -------------------------------
    def test_late_alert_does_not_create_two_root_states(self) -> None:
        # 迟到告警的事件时点早于恢复观测：按恢复关闭根因，迟到告警只作覆盖依据
        _, closed = self.server.request("/api/metricdrill/2/close", "POST", {})
        self.assertEqual(closed["entry"]["数据集"]["状态"], "已关闭")
        _, detail = self.server.request("/api/metricdrill/2")
        self.assertEqual(detail["当前根因状态"], "已关闭")
        self.assertNotEqual(detail["当前根因状态"], "告警中")

        _, dataset = self.server.request("/api/metricdrill/2/dataset")
        covered = dataset["迟到告警裁决"]["覆盖"]
        self.assertEqual(len(covered), 1)
        self.assertEqual(covered[0]["结论"], "被恢复观测覆盖")
        self.assertEqual(dataset["迟到告警裁决"]["采用"], [])

        # 恢复观测之后才到的异常：根因从已关闭转“观察中”，仍只有一套状态
        status, late = self.server.request(
            "/api/metricdrill/2/late-alerts",
            "POST",
            {"values": {
                "维度": "渠道", "维度值": "自然流量", "基线值": 95, "观测值": 90,
                "事件时点": "2026-11-17T09:10:00", "到达时点": "2026-11-17T09:20:00",
            }},
        )
        self.assertEqual(status, 200, late)
        self.assertTrue(late["ok"])
        _, board = self.server.request("/api/dashboard/3")
        _, drill = self.server.request("/api/metricdrill/2")
        self.assertEqual(board["当前根因状态"], "观察中")
        self.assertEqual(drill["当前根因状态"], "观察中")
        self.assertEqual(board["告警线状态"], "告警线内")
        _, reconcile = self.server.request("/api/metricdrill/2/reconcile")
        self.assertTrue(reconcile["两侧根因一致"])

        # 历史区同时保留原告警排名与迟到告警裁决
        _, history = self.server.request("/api/metricdrill/2/history")
        types = [item["历史类型"] for item in history["items"]]
        self.assertIn("原告警时点排名", types)
        self.assertTrue(any("重开观察" in name for name in types))
        original = next(item for item in history["items"] if item["历史类型"] == "原告警时点排名")
        self.assertEqual(original["贡献度排名"][1]["维度值"], "华东")

    # -- 重复关闭 + 高并发：只有一个有效数据集 ----------------------------
    def test_concurrent_requests_create_single_dataset(self) -> None:
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker(path: str) -> None:
            outcome = self.server.request(path, "POST", {})
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=("/api/metricdrill/2/close",)) for _ in range(12)]
        threads += [threading.Thread(target=worker, args=("/api/metricdrill/2/save-dataset",)) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(status == 200 for status, _ in results), results[:2])
        self.assertTrue(all(payload.get("ok") for _, payload in results))

        _, datasets = self.server.request("/api/metricdrill/datasets")
        self.assertEqual(datasets["total"], 1)
        self.assertEqual(datasets["items"][0]["处理状态"], "已处理")
        _, credentials = self.server.request("/api/metricdrill/credentials")
        self.assertEqual(credentials["total"], 1)

        # 重复关闭返回同一张凭证，不会再落一条
        _, again = self.server.request("/api/metricdrill/2/close", "POST", {})
        self.assertTrue(again["ok"])
        _, credentials = self.server.request("/api/metricdrill/credentials")
        self.assertEqual(credentials["total"], 1)

    # -- 告警线：指标在告警线内才允许关闭 --------------------------------
    def test_alert_line_status_is_exposed_consistently(self) -> None:
        _, closed = self.server.request("/api/metricdrill/2/close", "POST", {})
        self.assertTrue(closed["ok"])
        _, board = self.server.request("/api/dashboard/3")
        _, drill = self.server.request("/api/metricdrill/2")
        _, dataset = self.server.request("/api/metricdrill/2/dataset")
        self.assertEqual(board["告警线状态"], "告警线内")
        self.assertEqual(drill["告警线状态"], "告警线内")
        self.assertEqual(dataset["告警线状态"], "告警线内")
        self.assertEqual(board["指标观测值"], 93.6)
        self.assertEqual(board["告警线"], 92.0)
        self.assertEqual(dataset["当前根因状态"], "已关闭")

    # -- 刷新后可浏览关联明细（服务重启后仍能读到） -----------------------
    def test_reload_after_restart_keeps_dataset_and_credential(self) -> None:
        _, closed = self.server.request("/api/metricdrill/2/close", "POST", {})
        dataset_code = closed["entry"]["数据集"]["数据集编号"]

        self.server.stop()
        self.server.start()

        _, detail = self.server.request("/api/metricdrill/2")
        self.assertEqual(detail["当前根因状态"], "已关闭")
        self.assertEqual(detail["数据集编号"], dataset_code)
        _, dataset = self.server.request("/api/metricdrill/2/dataset")
        self.assertEqual(dataset["数据集编号"], dataset_code)
        _, credential = self.server.request("/api/metricdrill/2/credential")
        self.assertEqual(credential["数据集编号"], dataset_code)
        _, board = self.server.request("/api/dashboard/3")
        self.assertEqual(board["当前根因状态"], "已关闭")
        self.assertEqual(board["数据集编号"], dataset_code)

    # -- 导出接口不被 /{entry_id} 抢占 ------------------------------------
    def test_export_endpoints_are_reachable(self) -> None:
        for path in ("/api/dashboard/export", "/api/metricdrill/export"):
            status, payload = self.server.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn("items", payload)


if __name__ == "__main__":
    unittest.main()
