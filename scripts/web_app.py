#!/usr/bin/env python3
"""Local HTML control panel for the pure-Python BOSS batch scraper."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    from . import boss_cdp_raw as boss
    from . import batch_search as batch
except ImportError:  # Direct execution: python scripts/web_app.py
    import boss_cdp_raw as boss
    import batch_search as batch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "web" / "index.html"
SUPPORTED_TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm"}
LIST_PROGRESS_RE = re.compile(r"===\s*组合\s+(\d+)/(\d+):")
DETAIL_TOTAL_RE = re.compile(r"===\s*抓取岗位详情\s*\((\d+)\s*个\)")
DETAIL_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]")


class WebRequestError(ValueError):
    """A request from the local UI is invalid."""


@dataclass
class ScrapeTask:
    job_id: str
    mode: str
    table_path: str
    output_path: str
    command: list[str]
    csv_path: str = ""
    json_path: str = ""
    fetch_jd: bool = True
    state: str = "queued"
    phase: str = "准备启动"
    progress: int = 0
    logs: list[str] = field(default_factory=list)
    return_code: Optional[int] = None
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    detail_started: bool = False
    process: Optional[subprocess.Popen] = field(default=None, repr=False)

    def public_data(self, offset: int = 0) -> dict:
        safe_offset = max(0, min(offset, len(self.logs)))
        return {
            "job_id": self.job_id,
            "mode": self.mode,
            "table_path": self.table_path,
            "output_path": self.output_path,
            "csv_path": self.csv_path,
            "json_path": self.json_path,
            "state": self.state,
            "phase": self.phase,
            "progress": self.progress,
            "return_code": self.return_code,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": self.logs[safe_offset:],
            "next_offset": len(self.logs),
        }


def default_output_root() -> Path:
    return PROJECT_ROOT / "result"


def _boolean_value(value, default=True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WebRequestError("是否抓取 JD 必须是布尔值")


def normalize_start_request(payload: dict) -> dict:
    mode = str(payload.get("mode", "keyword")).strip().lower()
    if mode not in {"keyword", "company"}:
        raise WebRequestError("检索模式必须是 keyword 或 company")

    raw_table_path = str(payload.get("table_path", "")).strip()
    if not raw_table_path:
        raise WebRequestError("请输入检索规则表格路径")
    table_path = Path(raw_table_path).expanduser()
    if not table_path.is_absolute():
        table_path = PROJECT_ROOT / table_path
    table_path = table_path.resolve()
    if not table_path.is_file():
        raise WebRequestError(f"表格文件不存在: {table_path}")
    if table_path.suffix.lower() not in SUPPORTED_TABLE_SUFFIXES:
        raise WebRequestError("规则表格仅支持 CSV、TSV、XLSX 或 XLSM")

    raw_output_path = str(payload.get("output_path", "")).strip()
    if raw_output_path:
        output_root = Path(raw_output_path).expanduser()
        if not output_root.is_absolute():
            output_root = PROJECT_ROOT / output_root
        output_root = output_root.resolve()
    else:
        output_root = default_output_root()
    if output_root.exists() and not output_root.is_dir():
        raise WebRequestError("输出路径必须是文件夹，不能是文件")
    if not output_root.exists() and output_root.suffix.lower() in {".csv", ".json"}:
        raise WebRequestError("输出路径请填写文件夹，不要填写 CSV/JSON 文件名")

    raw_pages = payload.get("pages")
    if raw_pages in (None, ""):
        pages = None
    else:
        try:
            pages = int(raw_pages)
        except (TypeError, ValueError) as exc:
            raise WebRequestError("每个组合的页数必须是整数，或留空抓取全部页") from exc
        if pages < 1:
            raise WebRequestError("每个组合的页数必须是正整数，或留空抓取全部页")

    try:
        interval = float(payload.get("interval", 8))
    except (TypeError, ValueError) as exc:
        raise WebRequestError("抓取时间间隔必须是数字") from exc
    if interval < 0 or interval > 600:
        raise WebRequestError("抓取时间间隔必须在 0-600 秒之间")

    fetch_jd = _boolean_value(payload.get("fetch_jd"), default=True)

    max_details = payload.get("max_details") if fetch_jd else None
    if max_details in (None, ""):
        max_details = None
    else:
        try:
            max_details = int(max_details)
        except (TypeError, ValueError) as exc:
            raise WebRequestError("详情数量上限必须是整数") from exc
        if max_details <= 0:
            raise WebRequestError("详情数量上限必须大于 0")

    company_match = str(payload.get("company_match", "contains")).strip().lower()
    if company_match not in {"contains", "exact"}:
        raise WebRequestError("公司名校验策略必须是 contains 或 exact")

    raw_fields = payload.get("output_fields")
    if raw_fields is None:
        output_fields = list(batch.FINAL_CSV_COLUMNS)
    elif not isinstance(raw_fields, list):
        raise WebRequestError("输出字段必须是数组")
    else:
        output_fields = []
        for raw_field in raw_fields:
            field_name = str(raw_field).strip()
            if field_name and field_name not in output_fields:
                output_fields.append(field_name)
    if not output_fields:
        raise WebRequestError("请至少选择一个输出字段")
    unknown_fields = [
        field_name for field_name in output_fields
        if field_name not in batch.OUTPUT_FIELD_LABELS
    ]
    if unknown_fields:
        raise WebRequestError(f"不支持的输出字段: {', '.join(unknown_fields)}")

    return {
        "mode": mode,
        "table_path": table_path,
        "output_root": output_root,
        "pages": pages,
        "interval": interval,
        "fetch_jd": fetch_jd,
        "output_fields": output_fields,
        "max_details": max_details,
        "company_match": company_match,
    }


def build_command(options: dict) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "scripts.batch_search",
        str(options["table_path"]),
        "--mode",
        options["mode"],
        "--result-dir",
        str(options["result_dir"]),
        "--interval",
        str(options["interval"]),
        "--output-fields",
        ",".join(options["output_fields"]),
        "--company-match",
        options["company_match"],
    ]
    if options["pages"] is not None:
        command.extend(["--pages", str(options["pages"])])
    if options["fetch_jd"] and options["max_details"] is not None:
        command.extend(["--max-details", str(options["max_details"])])
    if not options["fetch_jd"]:
        command.append("--no-detail")
    return command


def update_progress_from_line(task: ScrapeTask, line: str) -> None:
    list_match = LIST_PROGRESS_RE.search(line)
    if list_match:
        current, total = (int(value) for value in list_match.groups())
        task.phase = f"检索职位列表 {current}/{total}"
        task.progress = max(task.progress, 8 + round(42 * current / max(total, 1)))
        return
    if "合并去重后共" in line:
        task.phase = "职位去重完成，准备抓取 JD" if task.fetch_jd else "职位去重完成，准备输出"
        task.progress = max(task.progress, 52)
        return
    if "已关闭 JD 抓取" in line:
        task.phase = "已跳过 JD，正在生成结果"
        task.progress = max(task.progress, 82)
        return
    detail_total = DETAIL_TOTAL_RE.search(line)
    if detail_total:
        task.detail_started = True
        task.phase = f"抓取岗位详情 0/{detail_total.group(1)}"
        task.progress = max(task.progress, 55)
        return
    if task.detail_started:
        detail_match = DETAIL_PROGRESS_RE.search(line)
        if detail_match:
            current, total = (int(value) for value in detail_match.groups())
            task.phase = f"抓取岗位详情 {current}/{total}"
            task.progress = max(task.progress, 55 + round(40 * current / max(total, 1)))
            return
    if "结果 CSV 已保存" in line:
        task.phase = "正在完成输出"
        task.progress = max(task.progress, 97)


class ScrapeTaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, ScrapeTask] = {}
        self._lock = threading.RLock()

    def start(self, payload: dict) -> ScrapeTask:
        options = normalize_start_request(payload)
        with self._lock:
            active = next(
                (task for task in self._tasks.values() if task.state in {"queued", "running"}),
                None,
            )
            if active:
                raise WebRequestError("已有检索任务正在运行，请等待完成或先停止任务")
            try:
                result_dir = batch.create_result_directory(
                    str(options["output_root"]),
                    str(options["table_path"]),
                    options["mode"],
                )
            except OSError as exc:
                raise WebRequestError(f"无法创建输出目录: {exc}") from exc
            options["result_dir"] = result_dir
            _metadata_path, csv_path, json_path = batch.output_paths(result_dir)
            task = ScrapeTask(
                job_id=uuid.uuid4().hex,
                mode=options["mode"],
                table_path=str(options["table_path"]),
                output_path=str(result_dir),
                command=build_command(options),
                csv_path=csv_path,
                json_path=json_path,
                fetch_jd=options["fetch_jd"],
            )
            self._tasks[task.job_id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def get(self, job_id: str) -> ScrapeTask:
        with self._lock:
            task = self._tasks.get(job_id)
            if not task:
                raise WebRequestError("检索任务不存在或服务已重启")
            return task

    def cancel(self, job_id: str) -> ScrapeTask:
        task = self.get(job_id)
        with self._lock:
            if task.state not in {"queued", "running"}:
                return task
            task.state = "cancelling"
            task.phase = "正在停止"
            process = task.process
        if process and process.poll() is None:
            process.terminate()
        return task

    def _run(self, task: ScrapeTask) -> None:
        with self._lock:
            task.state = "running"
            task.phase = "启动检索程序"
            task.progress = 3
            task.started_at = datetime.now().isoformat(timespec="seconds")
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                task.command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
            )
            with self._lock:
                task.process = process
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                with self._lock:
                    task.logs.append(line)
                    update_progress_from_line(task, line)
            return_code = process.wait()
            with self._lock:
                task.return_code = return_code
                if task.state == "cancelling":
                    task.state = "cancelled"
                    task.phase = "任务已停止"
                elif return_code == 0:
                    task.state = "completed"
                    task.phase = "检索完成"
                    task.progress = 100
                else:
                    task.state = "failed"
                    task.phase = "检索失败"
                    task.error = task.logs[-1] if task.logs else f"进程退出码: {return_code}"
                task.finished_at = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:  # Surface server/process errors to the UI.
            with self._lock:
                task.state = "failed"
                task.phase = "检索失败"
                task.error = str(exc)
                task.logs.append(f"Web 服务启动检索失败: {exc}")
                task.finished_at = datetime.now().isoformat(timespec="seconds")


TASK_MANAGER = ScrapeTaskManager()


class WebHandler(BaseHTTPRequestHandler):
    server_version = "BossScraperWeb/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html()
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0]
            try:
                offset = int(query.get("offset", ["0"])[0])
                task = TASK_MANAGER.get(job_id)
                self._send_json(task.public_data(offset))
            except (ValueError, WebRequestError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/start":
                task = TASK_MANAGER.start(payload)
                self._send_json(task.public_data(), HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/cancel":
                task = TASK_MANAGER.cancel(str(payload.get("job_id", "")))
                self._send_json(task.public_data())
                return
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except WebRequestError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "请求内容不是有效 JSON"}, HTTPStatus.BAD_REQUEST)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WebRequestError("Content-Length 无效") from exc
        if length <= 0 or length > 64 * 1024:
            raise WebRequestError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise WebRequestError("请求内容必须是 JSON 对象")
        return payload

    def _send_html(self) -> None:
        if not INDEX_PATH.is_file():
            self._send_json({"error": f"HTML 入口不存在: {INDEX_PATH}"}, HTTPStatus.NOT_FOUND)
            return
        body = INDEX_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BOSS 批量检索本地 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not INDEX_PATH.is_file():
        print(f"❌ HTML 入口不存在: {INDEX_PATH}")
        return 1
    try:
        server = ThreadingHTTPServer((args.host, args.port), WebHandler)
    except OSError as exc:
        print(f"❌ Web 服务启动失败: {exc}")
        return 1
    url = f"http://{args.host}:{server.server_port}/"
    print(f"BOSS 检索控制台已启动: {url}")
    print("按 Ctrl+C 停止服务")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWeb 服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
