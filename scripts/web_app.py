#!/usr/bin/env python3
"""Local HTML control panel for the pure-Python BOSS batch scraper."""

from __future__ import annotations

import argparse
import json
import math
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
DEFAULT_LLM_JOB_REQUIREMENTS = "我们寻找和AI相关或对AI算力有潜在需求的岗位"
LIST_PROGRESS_RE = re.compile(r"===\s*组合\s+(\d+)/(\d+):")
DETAIL_TOTAL_RE = re.compile(r"===\s*抓取岗位详情\s*\((\d+)\s*个\)")
DETAIL_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]")
PUBLISH_TOTAL_RE = re.compile(r"===\s*读取岗位发布时间\s*\((\d+)\s*个\)")
PUBLISH_PROGRESS_RE = re.compile(r"^\[发布时间\s+(\d+)/(\d+)\]")
CHECKPOINT_PROGRESS_RE = re.compile(
    r"已加载检查点:.*列表组合\s+(\d+)/(\d+)，发布时间\s+(\d+)/(\d+)"
)
DETAIL_CHECKPOINT_RE = re.compile(r"已加载详情检查点:\s*(\d+)/(\d+)")


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
    companies_csv_path: str = ""
    fetch_jd: bool = False
    state: str = "queued"
    phase: str = "准备启动"
    progress: int = 0
    logs: list[str] = field(default_factory=list)
    return_code: Optional[int] = None
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    detail_started: bool = False
    publish_started: bool = False
    resumed_from: str = ""
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
            "companies_csv_path": self.companies_csv_path,
            "state": self.state,
            "phase": self.phase,
            "progress": self.progress,
            "return_code": self.return_code,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": self.logs[safe_offset:],
            "next_offset": len(self.logs),
            "can_resume": self.state in {"failed", "cancelled"},
            "resumed_from": self.resumed_from,
        }


def default_output_root() -> Path:
    return PROJECT_ROOT / "result"


def _boolean_value(value, default=True, label="配置项") -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WebRequestError(f"{label}必须是布尔值")


def _date_value(value, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise WebRequestError(f"{label}必须是 YYYY-MM-DD 格式") from exc


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
        interval = float(payload.get("interval", 3))
    except (TypeError, ValueError) as exc:
        raise WebRequestError("抓取时间间隔必须是数字") from exc
    if interval < 0 or interval > 600:
        raise WebRequestError("抓取时间间隔必须在 0-600 秒之间")

    try:
        keyword_match_threshold = float(payload.get(
            "keyword_match_threshold",
            boss.KEYWORD_FUZZY_MATCH_THRESHOLD,
        ))
    except (TypeError, ValueError) as exc:
        raise WebRequestError("岗位关键词模糊匹配阈值必须是数字") from exc
    if not math.isfinite(keyword_match_threshold) or not 0 <= keyword_match_threshold <= 1:
        raise WebRequestError("岗位关键词模糊匹配阈值必须在 0-1 之间")

    fetch_jd = _boolean_value(
        payload.get("fetch_jd"), default=False, label="是否抓取 JD",
    )

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

    published_from = _date_value(payload.get("published_from"), "发布时间起始日期")
    published_to = _date_value(payload.get("published_to"), "发布时间结束日期")
    if published_from and published_to and published_from > published_to:
        raise WebRequestError("发布时间起始日期不能晚于结束日期")
    fetch_publish_time = _boolean_value(
        payload.get("fetch_publish_time"),
        default=bool(published_from or published_to),
        label="是否查询岗位发布时间",
    )
    if not fetch_publish_time:
        published_from = ""
        published_to = ""

    job_requirements = str(payload.get("job_requirements") or "").strip()
    if len(job_requirements) > 10000:
        raise WebRequestError("岗位需求不能超过 10000 个字符")
    llm_filter_enabled = _boolean_value(
        payload.get("llm_filter_enabled"),
        default=True,
        label="是否启用 LLM 岗位相关性筛选",
    )
    if llm_filter_enabled and not job_requirements:
        job_requirements = DEFAULT_LLM_JOB_REQUIREMENTS
    elif not llm_filter_enabled:
        job_requirements = ""

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
        "keyword_match_threshold": keyword_match_threshold,
        "fetch_jd": fetch_jd,
        "fetch_publish_time": fetch_publish_time,
        "output_fields": output_fields,
        "max_details": max_details,
        "company_match": company_match,
        "published_from": published_from,
        "published_to": published_to,
        "llm_filter_enabled": llm_filter_enabled,
        "job_requirements": job_requirements,
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
        "--keyword-match-threshold",
        str(options.get(
            "keyword_match_threshold",
            boss.KEYWORD_FUZZY_MATCH_THRESHOLD,
        )),
        "--output-fields",
        ",".join(options["output_fields"]),
        "--company-match",
        options["company_match"],
    ]
    if options["pages"] is not None:
        command.extend(["--pages", str(options["pages"])])
    command.append(
        "--fetch-publish-time"
        if options.get("fetch_publish_time", False)
        else "--no-fetch-publish-time"
    )
    if options.get("published_from"):
        command.extend(["--published-from", options["published_from"]])
    if options.get("published_to"):
        command.extend(["--published-to", options["published_to"]])
    if options["fetch_jd"] and options["max_details"] is not None:
        command.extend(["--max-details", str(options["max_details"])])
    command.append("--fetch-detail" if options["fetch_jd"] else "--no-detail")
    if options.get("job_requirements"):
        command.extend(["--job-requirements", options["job_requirements"]])
    return command


def update_progress_from_line(task: ScrapeTask, line: str) -> None:
    detail_checkpoint = DETAIL_CHECKPOINT_RE.search(line)
    if detail_checkpoint:
        completed, total = (int(value) for value in detail_checkpoint.groups())
        task.detail_started = True
        task.phase = f"从断点继续抓取详情 {completed}/{total}"
        task.progress = max(
            task.progress,
            70 + round(25 * completed / max(total, 1)),
        )
        return
    checkpoint = CHECKPOINT_PROGRESS_RE.search(line)
    if checkpoint:
        completed, total, publish_completed, publish_total = (
            int(value) for value in checkpoint.groups()
        )
        if publish_completed:
            task.publish_started = True
            task.phase = f"从断点继续读取发布时间 {publish_completed}/{publish_total}"
            task.progress = max(
                task.progress,
                52 + round(16 * publish_completed / max(publish_total, 1)),
            )
        else:
            task.phase = f"从断点继续列表检索 {completed}/{total}"
            task.progress = max(
                task.progress,
                8 + round(42 * completed / max(total, 1)),
            )
        return
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
    publish_total = PUBLISH_TOTAL_RE.search(line)
    if publish_total:
        task.publish_started = True
        task.phase = f"读取岗位发布时间 0/{publish_total.group(1)}"
        task.progress = max(task.progress, 52)
        return
    publish_progress = PUBLISH_PROGRESS_RE.search(line)
    if publish_progress:
        current, total = (int(value) for value in publish_progress.groups())
        task.phase = f"读取岗位发布时间 {current}/{total}"
        task.progress = max(task.progress, 52 + round(16 * current / max(total, 1)))
        return
    detail_total = DETAIL_TOTAL_RE.search(line)
    if detail_total:
        task.detail_started = True
        task.phase = f"抓取岗位详情 0/{detail_total.group(1)}"
        task.progress = max(task.progress, 70 if task.publish_started else 55)
        return
    if task.detail_started:
        detail_match = DETAIL_PROGRESS_RE.search(line)
        if detail_match:
            current, total = (int(value) for value in detail_match.groups())
            task.phase = f"抓取岗位详情 {current}/{total}"
            start = 70 if task.publish_started else 55
            task.progress = max(
                task.progress,
                start + round((95 - start) * current / max(total, 1)),
            )
            return
    if "结果 CSV 已保存" in line:
        task.phase = "正在完成输出"
        task.progress = max(task.progress, 97)
    elif "=== LLM 语义理解岗位" in line:
        task.phase = "LLM 正在语义理解岗位"
        task.progress = max(task.progress, 90)
    elif "=== 抓取公司工商信息" in line:
        task.phase = "正在抓取公司全称和信用代码"
        task.progress = max(task.progress, 94)


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
                companies_csv_path=(
                    str(result_dir / "companies.csv")
                    if options["job_requirements"] else ""
                ),
                fetch_jd=options["fetch_jd"],
            )
            self._tasks[task.job_id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def resume(self, job_id: str) -> ScrapeTask:
        """Restart a terminal task with its original command and result directory.

        ``batch_search`` owns the durable checkpoint. Reusing the exact command
        lets it load completed combinations/publication dates instead of
        creating a fresh search plan or output directory.
        """
        previous = self.get(job_id)
        with self._lock:
            active = next(
                (task for task in self._tasks.values() if task.state in {
                    "queued", "running", "cancelling",
                }),
                None,
            )
            if active:
                raise WebRequestError("已有检索任务正在运行，请等待完成或先停止任务")
            if previous.state not in {"failed", "cancelled"}:
                raise WebRequestError("只能继续已失败或已停止的任务")
            task = ScrapeTask(
                job_id=uuid.uuid4().hex,
                mode=previous.mode,
                table_path=previous.table_path,
                output_path=previous.output_path,
                command=list(previous.command),
                csv_path=previous.csv_path,
                json_path=previous.json_path,
                companies_csv_path=previous.companies_csv_path,
                fetch_jd=previous.fetch_jd,
                phase="准备从检查点继续",
                resumed_from=previous.job_id,
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

    def switch_account(self) -> str:
        """Clear only the dedicated BOSS session while no scrape is active."""
        with self._lock:
            active = next(
                (task for task in self._tasks.values() if task.state in {
                    "queued", "running", "cancelling",
                }),
                None,
            )
            if active:
                raise WebRequestError("检索任务运行期间不能切换账号，请先停止任务")
            try:
                return boss.switch_boss_account()
            except (boss.CDPConnectionError, RuntimeError, OSError) as exc:
                raise WebRequestError(f"切换账号失败: {exc}") from exc

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
            if parsed.path == "/api/resume":
                task = TASK_MANAGER.resume(str(payload.get("job_id", "")))
                self._send_json(task.public_data(), HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/switch-account":
                login_url = TASK_MANAGER.switch_account()
                self._send_json({
                    "message": "已退出当前账号，请在 BOSS 专用 Chrome 中登录新账号",
                    "login_url": login_url,
                })
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
