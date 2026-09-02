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
        self.assertEqual(options["interval"], 8)
        self.assertTrue(options["fetch_jd"])
        self.assertIn("salary", options["output_fields"])
        self.assertIn("experience", options["output_fields"])

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
        self.assertIn("--output-fields", command)
        self.assertIn("--no-detail", command)

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


class HtmlContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (module.PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_rule_and_safety_disclosures_are_present(self):
        self.assertIn("检索规则要求", self.html)
        self.assertIn("计算方式：", self.html)
        self.assertIn("安全保护条例", self.html)
        self.assertIn("搜索组合默认最多 64 个", self.html)

    def test_removed_path_hint_and_ten_page_input_cap(self):
        self.assertNotIn("浏览器无法读取文件的真实路径", self.html)
        self.assertNotIn('id="pages" type="number" min="1" max="10"', self.html)


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

    def test_csv_line_reaches_finishing_phase(self):
        module.update_progress_from_line(self.task, "结果 CSV 已保存: /tmp/jobs.csv")
        self.assertEqual(self.task.phase, "正在完成输出")
        self.assertEqual(self.task.progress, 97)


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
