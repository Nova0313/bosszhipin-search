import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import web_app as module


class RequestValidationTests(unittest.TestCase):
    def test_keyword_request_resolves_paths_and_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "mode": "keyword", "table_path": str(table), "pages": "2",
                "output_path": str(pathlib.Path(temp_dir) / "results"),
            })

        self.assertEqual(options["mode"], "keyword")
        self.assertEqual(options["pages"], 2)
        self.assertEqual(options["output_root"].name, "results")
        self.assertIsNone(options["max_details"])
        self.assertEqual(options["interval"], 3)
        self.assertEqual(options["keyword_match_threshold"], 0.8)
        self.assertFalse(options["fetch_jd"])
        self.assertFalse(options["fetch_publish_time"])
        self.assertTrue(options["llm_filter_enabled"])
        self.assertEqual(
            options["job_requirements"], module.DEFAULT_LLM_JOB_REQUIREMENTS,
        )
        self.assertEqual(options["published_from"], "")
        self.assertEqual(options["published_to"], "")
        self.assertEqual(options["output_fields"], [
            "job_id", "title", "location", "salary", "experience",
            "company_scale", "company_stage", "company_industry",
        ])

    def test_missing_table_is_rejected(self):
        with self.assertRaisesRegex(module.WebRequestError, "表格文件不存在"):
            module.normalize_start_request({
                "mode": "keyword", "table_path": "/no/such/table.csv",
            })

    def test_invalid_output_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(module.WebRequestError, "请填写文件夹"):
                module.normalize_start_request({
                    "mode": "keyword", "table_path": str(table),
                    "output_path": str(pathlib.Path(temp_dir) / "result.json"),
                })

    def test_build_command_never_uses_shell_string(self):
        options = {
            "mode": "company",
            "table_path": pathlib.Path("/tmp/company rules.csv"),
            "result_dir": pathlib.Path("/tmp/company jobs"),
            "pages": 3,
            "interval": 6.5,
            "fetch_jd": False,
            "output_fields": ["title", "salary", "experience"],
            "max_details": 12,
            "company_match": "exact",
        }
        command = module.build_command(options)
        self.assertIsInstance(command, list)
        self.assertIn("scripts.batch_search", command)
        self.assertIn("/tmp/company rules.csv", command)
        self.assertNotIn("--max-details", command)
        self.assertIn("--interval", command)
        self.assertIn("--keyword-match-threshold", command)
        self.assertIn("--output-fields", command)
        self.assertIn("--no-detail", command)
        self.assertNotIn("--fetch-detail", command)
        self.assertIn("--no-fetch-publish-time", command)

    def test_build_command_explicitly_enables_jd_fetching(self):
        options = {
            "mode": "keyword",
            "table_path": pathlib.Path("/tmp/rules.csv"),
            "result_dir": pathlib.Path("/tmp/jobs"),
            "pages": None,
            "interval": 3,
            "fetch_jd": True,
            "fetch_publish_time": False,
            "output_fields": ["job_id", "jd"],
            "max_details": None,
            "company_match": "contains",
        }
        command = module.build_command(options)
        self.assertIn("--fetch-detail", command)
        self.assertNotIn("--no-detail", command)

    def test_request_validates_interval_jd_and_output_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "interval": "2.5",
                "fetch_jd": False,
                "max_details": "9",
                "output_fields": ["title", "salary", "experience"],
            })

        self.assertEqual(options["interval"], 2.5)
        self.assertIsNone(options["pages"])
        self.assertFalse(options["fetch_jd"])
        self.assertIsNone(options["max_details"])
        self.assertEqual(options["output_fields"], ["title", "salary", "experience"])

    def test_keyword_match_threshold_is_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "keyword_match_threshold": "0.82",
            })
            self.assertEqual(options["keyword_match_threshold"], 0.82)

            for invalid in ("-0.01", "1.01", "NaN"):
                with self.assertRaisesRegex(module.WebRequestError, "0-1"):
                    module.normalize_start_request({
                        "table_path": str(table),
                        "keyword_match_threshold": invalid,
                    })

    def test_manual_pages_has_no_ten_page_ceiling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "pages": "25",
            })

        self.assertEqual(options["pages"], 25)

    def test_manual_pages_must_still_be_positive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            with self.assertRaisesRegex(module.WebRequestError, "正整数"):
                module.normalize_start_request({
                    "table_path": str(table),
                    "pages": "0",
                })

    def test_publish_date_range_is_validated_and_forwarded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "published_from": "2026-08-01",
                "published_to": "2026-08-31",
            })

        command = module.build_command({**options, "result_dir": pathlib.Path("/tmp/result")})
        self.assertEqual(options["published_from"], "2026-08-01")
        self.assertEqual(options["published_to"], "2026-08-31")
        self.assertIn("--published-from", command)
        self.assertIn("2026-08-01", command)
        self.assertIn("--published-to", command)
        self.assertIn("2026-08-31", command)

    def test_publish_time_lookup_choice_is_forwarded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "fetch_publish_time": True,
            })

        command = module.build_command({**options, "result_dir": pathlib.Path("/tmp/result")})
        self.assertTrue(options["fetch_publish_time"])
        self.assertIn("--fetch-publish-time", command)
        self.assertNotIn("--no-fetch-publish-time", command)

    def test_explicitly_disabled_publish_lookup_ignores_date_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市\nPython,上海\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "fetch_publish_time": False,
                "published_from": "2026-08-01",
                "published_to": "2026-08-31",
            })

        self.assertEqual(options["published_from"], "")
        self.assertEqual(options["published_to"], "")

    def test_publish_date_range_rejects_reversed_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            with self.assertRaisesRegex(module.WebRequestError, "不能晚于"):
                module.normalize_start_request({
                    "table_path": str(table),
                    "published_from": "2026-09-01",
                    "published_to": "2026-08-31",
                })

    def test_job_requirements_are_validated_and_forwarded_without_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            options = module.normalize_start_request({
                "table_path": str(table),
                "job_requirements": "  只要机器人视觉岗  ",
            })

        command = module.build_command({**options, "result_dir": pathlib.Path("/tmp/result")})
        self.assertEqual(options["job_requirements"], "只要机器人视觉岗")
        self.assertIn("--job-requirements", command)
        self.assertIn("只要机器人视觉岗", command)
        self.assertNotIn("OPENAI_API_KEY", command)

    def test_job_requirements_length_is_limited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            with self.assertRaisesRegex(module.WebRequestError, "10000"):
                module.normalize_start_request({
                    "table_path": str(table),
                    "job_requirements": "x" * 10001,
                })

    def test_llm_toggle_supplies_default_requirement_and_disables_stale_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("搜索关键词,城市\nPython,上海\n", encoding="utf-8")
            enabled = module.normalize_start_request({
                "table_path": str(table), "llm_filter_enabled": True,
            })
            disabled = module.normalize_start_request({
                "table_path": str(table),
                "llm_filter_enabled": False,
                "job_requirements": "不应启用",
            })

        self.assertEqual(
            enabled["job_requirements"], module.DEFAULT_LLM_JOB_REQUIREMENTS,
        )
        self.assertEqual(disabled["job_requirements"], "")


class HtmlContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (module.PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_rule_and_safety_disclosures_are_present(self):
        self.assertIn("检索规则要求", self.html)
        self.assertIn("计算方式：", self.html)
        self.assertIn("安全保护条例", self.html)
        self.assertIn("搜索组合默认最多 64 个", self.html)
        self.assertIn("全局请求设置 5000 次", self.html)
        self.assertNotIn("全局请求设置 500 次", self.html)

    def test_removed_path_hint_and_ten_page_input_cap(self):
        self.assertNotIn("浏览器无法读取文件的真实路径", self.html)
        self.assertNotIn('id="pages" type="number" min="1" max="10"', self.html)

    def test_publish_date_controls_and_output_field_are_present(self):
        self.assertIn('id="published-from" type="date" disabled', self.html)
        self.assertIn('id="published-to" type="date" disabled', self.html)
        self.assertIn('id="fetch-publish-time" type="checkbox"', self.html)
        self.assertNotIn('value="publish_date" checked', self.html)
        self.assertIn('id="publish-date-range-field" hidden', self.html)
        self.assertIn("publishDateRangeField.hidden = !enabled", self.html)
        self.assertIn("syncPublishTimeControls", self.html)

    def test_keyword_match_threshold_control_is_present(self):
        self.assertIn('id="keyword-match-threshold"', self.html)
        self.assertIn('step="0.01" value="0.8"', self.html)
        self.assertIn("keyword_match_threshold: keywordMatchThreshold.value", self.html)

    def test_llm_requirement_and_company_output_controls_are_present(self):
        self.assertIn('id="llm-filter-enabled" type="checkbox" checked', self.html)
        self.assertIn('id="job-requirements"', self.html)
        self.assertIn("job_requirements: llmFilterEnabled.checked ? jobRequirements.value : ''", self.html)
        self.assertIn(module.DEFAULT_LLM_JOB_REQUIREMENTS, self.html)
        self.assertIn("原检索流程保持不变", self.html)
        self.assertIn("再按下方开关决定是否抓取 JD", self.html)
        self.assertIn("companies.csv", self.html)
        self.assertIn("companies_csv_path", self.html)
        advanced_start = self.html.index("<details>")
        advanced_end = self.html.index("</details>", advanced_start)
        llm_toggle = self.html.index('id="llm-filter-enabled"')
        self.assertLess(advanced_start, llm_toggle)
        self.assertLess(llm_toggle, advanced_end)
        self.assertIn("jobRequirementsField.hidden = !enabled", self.html)

    def test_default_interval_jd_and_output_fields_are_present(self):
        self.assertIn('id="interval" type="number" min="0" max="600" step="0.5" value="3"', self.html)
        self.assertIn('id="fetch-jd" type="checkbox"', self.html)
        self.assertNotIn('id="fetch-jd" type="checkbox" checked', self.html)
        self.assertIn('id="max-details-field" hidden', self.html)
        self.assertIn("maxDetailsField.hidden = !enabled", self.html)
        for field_name in (
            "job_id", "title", "location", "salary", "experience",
            "company_scale", "company_stage", "company_industry",
        ):
            self.assertIn(f'value="{field_name}" checked', self.html)
        self.assertNotIn('value="company" checked', self.html)
        self.assertIn('value="location" checked>公司地点', self.html)

    def test_switch_account_entry_and_confirmation_are_present(self):
        self.assertIn('id="switch-account-button"', self.html)
        self.assertIn("/api/switch-account", self.html)
        self.assertIn("不会影响你的主 Chrome", self.html)

    def test_interrupted_task_resume_entry_is_present(self):
        self.assertIn('id="resume-button"', self.html)
        self.assertIn("/api/resume", self.html)
        self.assertIn("继续中断任务", self.html)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.task = module.ScrapeTask("id", "keyword", "rules.csv", "jobs.csv", [])

    def test_list_progress_updates_phase_and_percentage(self):
        module.update_progress_from_line(self.task, "=== 组合 2/4: Python AND 上海 ===")
        self.assertEqual(self.task.phase, "检索职位列表 2/4")
        self.assertGreaterEqual(self.task.progress, 29)
        self.assertLess(self.task.progress, 55)

    def test_detail_progress_uses_second_half_of_bar(self):
        module.update_progress_from_line(self.task, "=== 抓取岗位详情 (10 个) ===")
        module.update_progress_from_line(self.task, "[5/10] 示例公司 - Python工程师")
        self.assertEqual(self.task.phase, "抓取岗位详情 5/10")
        self.assertEqual(self.task.progress, 75)

    def test_publish_time_progress_is_reported_before_detail_progress(self):
        module.update_progress_from_line(self.task, "=== 读取岗位发布时间 (10 个) ===")
        module.update_progress_from_line(self.task, "[发布时间 5/10] 示例公司 - Python工程师")
        self.assertEqual(self.task.phase, "读取岗位发布时间 5/10")
        self.assertEqual(self.task.progress, 60)

        module.update_progress_from_line(self.task, "=== 抓取岗位详情 (4 个) ===")
        module.update_progress_from_line(self.task, "[2/4] 示例公司 - Python工程师")
        self.assertEqual(self.task.phase, "抓取岗位详情 2/4")
        self.assertGreaterEqual(self.task.progress, 82)

    def test_csv_line_reaches_finishing_phase(self):
        module.update_progress_from_line(self.task, "结果 CSV 已保存: /tmp/jobs.csv")
        self.assertEqual(self.task.phase, "正在完成输出")
        self.assertEqual(self.task.progress, 97)

    def test_loaded_checkpoint_updates_resume_phase(self):
        module.update_progress_from_line(
            self.task,
            "已加载检查点: 候选岗位 30 条，列表组合 2/5，发布时间 0/30",
        )
        self.assertEqual(self.task.phase, "从断点继续列表检索 2/5")
        self.assertGreater(self.task.progress, 20)

    def test_loaded_detail_checkpoint_updates_resume_phase(self):
        module.update_progress_from_line(self.task, "已加载详情检查点: 8/20")
        self.assertEqual(self.task.phase, "从断点继续抓取详情 8/20")
        self.assertTrue(self.task.detail_started)


class ManagerTests(unittest.TestCase):
    def test_only_one_scrape_can_run_at_a_time(self):
        manager = module.ScrapeTaskManager()
        manager._tasks["active"] = module.ScrapeTask(
            "active", "keyword", "rules.csv", "jobs.csv", [], state="running"
        )
        with mock.patch.object(module, "normalize_start_request"):
            with self.assertRaisesRegex(module.WebRequestError, "已有检索任务"):
                manager.start({})

    def test_start_creates_isolated_result_folder_with_csv_and_json_paths(self):
        manager = module.ScrapeTaskManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            table = root / "rules.csv"
            table.write_text("搜索关键词,城市,薪资待遇,工作经验\nPython,上海,,\n", encoding="utf-8")
            with mock.patch.object(module.threading, "Thread") as thread:
                task = manager.start({
                    "table_path": str(table),
                    "output_path": str(root / "outputs"),
                    "fetch_jd": False,
                })

            result_dir = pathlib.Path(task.output_path)
            self.assertTrue(result_dir.is_dir())
            self.assertEqual(result_dir.parent, (root / "outputs").resolve())
            self.assertEqual(pathlib.Path(task.csv_path), result_dir / "jobs.csv")
            self.assertEqual(pathlib.Path(task.json_path), result_dir / "jobs.json")
            thread.return_value.start.assert_called_once_with()

    def test_switch_account_is_blocked_while_scrape_is_active(self):
        manager = module.ScrapeTaskManager()
        manager._tasks["active"] = module.ScrapeTask(
            "active", "keyword", "rules.csv", "jobs.csv", [], state="running"
        )
        with mock.patch.object(module.boss, "switch_boss_account") as switch:
            with self.assertRaisesRegex(module.WebRequestError, "请先停止任务"):
                manager.switch_account()
        switch.assert_not_called()

    def test_switch_account_uses_dedicated_chrome_helper(self):
        manager = module.ScrapeTaskManager()
        with mock.patch.object(
            module.boss,
            "switch_boss_account",
            return_value="https://login.zhipin.com/",
        ) as switch:
            login_url = manager.switch_account()
        self.assertEqual(login_url, "https://login.zhipin.com/")
        switch.assert_called_once_with()

    def test_failed_task_can_resume_with_same_command_and_result_directory(self):
        manager = module.ScrapeTaskManager()
        previous = module.ScrapeTask(
            "failed-id",
            "keyword",
            "/tmp/rules.csv",
            "/tmp/result/task-1",
            ["python", "-m", "scripts.batch_search", "--result-dir", "/tmp/result/task-1"],
            csv_path="/tmp/result/task-1/jobs.csv",
            json_path="/tmp/result/task-1/jobs.json",
            state="failed",
        )
        manager._tasks[previous.job_id] = previous

        with mock.patch.object(module.threading, "Thread") as thread:
            resumed = manager.resume(previous.job_id)

        self.assertNotEqual(resumed.job_id, previous.job_id)
        self.assertEqual(resumed.command, previous.command)
        self.assertEqual(resumed.output_path, previous.output_path)
        self.assertEqual(resumed.resumed_from, previous.job_id)
        self.assertFalse(resumed.public_data()["can_resume"])
        thread.return_value.start.assert_called_once_with()

    def test_completed_task_cannot_resume(self):
        manager = module.ScrapeTaskManager()
        manager._tasks["done"] = module.ScrapeTask(
            "done", "keyword", "rules.csv", "result", [], state="completed",
        )
        with self.assertRaisesRegex(module.WebRequestError, "只能继续"):
            manager.resume("done")


class PackagingTests(unittest.TestCase):
    def test_web_entrypoint_and_html_are_packaged(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('boss-web = "scripts.web_app:main"', pyproject)
        self.assertIn('"web/index.html" = "web/index.html"', pyproject)
        self.assertIn("/api/start", html)
        self.assertIn("progressbar", html)
        self.assertIn("留空时会抓取该组合的全部页", html)


if __name__ == "__main__":
    unittest.main()
