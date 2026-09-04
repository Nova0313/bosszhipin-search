import contextlib
import csv
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from datetime import date
from unittest import mock

from scripts import batch_search as module


def fake_city_resolver(value):
    mapping = {
        "上海": ("上海", "101020100"),
        "杭州": ("杭州", "101210100"),
        "北京": ("北京", "101010100"),
        "全国": ("全国", "100010000"),
    }
    return mapping[value]


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows(rows)


def write_minimal_xlsx(path, rows, sheet_name="搜索条件"):
    strings = []
    string_index = {}
    for row in rows:
        for value in row:
            text = str(value)
            if text not in string_index:
                string_index[text] = len(strings)
                strings.append(text)

    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{text}</t></si>" for text in strings)
        + "</sst>"
    )

    def column_name(index):
        result = ""
        value = index + 1
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(ord("A") + remainder) + result
        return result

    sheet_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(row):
            ref = f"{column_name(column_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="s"><v>{string_index[str(value)]}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


class TagParsingTests(unittest.TestCase):
    def test_help_describes_randomized_request_interval(self):
        help_text = module.build_arg_parser().format_help()

        self.assertIn("±20%", help_text)
        self.assertIn("以 10 秒为基准随机等待", help_text)

    def test_split_tags_supports_common_separators_and_deduplicates(self):
        self.assertEqual(
            module.split_tags("Python, Go；Java|Python\nRust、C++"),
            ["Python", "Go", "Java", "Rust", "C++"],
        )

    def test_friendly_range_aliases_expand_to_filter_codes(self):
        salary = module.resolve_filter_labels(
            ["20K以上"], module.boss.SALARY_MAP, module.SALARY_ALIASES, "薪资"
        )
        experience = module.resolve_filter_labels(
            ["3年以上"], module.boss.EXPERIENCE_MAP, module.EXPERIENCE_ALIASES, "经验"
        )
        self.assertEqual(salary, ("406", "407"))
        self.assertEqual(experience, ("105", "106", "107"))

    def test_blank_constraint_is_unlimited(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "", ""],
        ]
        rule = module.parse_rules(rows)[0]
        self.assertEqual(rule.salary_codes, ("",))
        self.assertEqual(rule.experience_codes, ("",))
        self.assertEqual(rule.job_type_codes, ("",))

    def test_full_time_job_type_is_forwarded_as_boss_filter(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验", "求职类型"],
            ["Python", "全国", "不限", "不限", "全职"],
        ]
        rule = module.parse_rules(rows)[0]
        combinations = module.expand_rules(
            [rule], city_resolver=fake_city_resolver
        )

        self.assertEqual(rule.job_type_codes, ("1901",))
        self.assertEqual(combinations[0].filters()["jobType"], "1901")
        self.assertEqual(combinations[0].to_dict()["job_type"], "全职")

    def test_unknown_filter_label_fails_before_browser_use(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "年薪百万", "3-5年"],
        ]
        with self.assertRaisesRegex(module.TableRuleError, "年薪百万"):
            module.parse_rules(rows)

    def test_company_mode_accepts_company_column_and_cell_or(self):
        rows = [
            ["公司名称", "城市", "薪资待遇", "工作经验"],
            ["字节跳动|腾讯", "北京", "20-50K", "3-5年"],
        ]
        rule = module.parse_rules(rows, mode=module.MODE_COMPANY)[0]
        self.assertEqual(rule.mode, module.MODE_COMPANY)
        self.assertEqual(rule.search_terms, ("字节跳动", "腾讯"))


class TableReadingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ["规则名称", "搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["后端", "Python,Go", "上海|杭州", "20-50K", "3-5年,5-10年"],
        ]

    def test_csv_table_expands_cell_or_and_column_and(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "rules.csv"
            write_csv(path, self.rows)
            rules, combinations = module.build_plan(
                str(path), max_combinations=32, city_resolver=fake_city_resolver
            )

        self.assertEqual(len(rules), 1)
        self.assertEqual(len(combinations), 8)  # 2 keywords * 2 cities * 1 salary * 2 exp
        actual = {
            (item.search_term, item.city_name, item.salary_code, item.experience_code)
            for item in combinations
        }
        self.assertIn(("Python", "上海", "406", "105"), actual)
        self.assertIn(("Go", "杭州", "406", "106"), actual)

    def test_xlsx_is_read_without_excel_library_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "rules.xlsx"
            write_minimal_xlsx(path, self.rows)
            rules, combinations = module.build_plan(
                str(path), sheet_name="搜索条件", max_combinations=32,
                city_resolver=fake_city_resolver,
            )

        self.assertEqual(rules[0].search_terms, ("Python", "Go"))
        self.assertEqual(len(combinations), 8)

    def test_columns_are_independent_or_pools_with_unequal_lengths(self):
        rows = [
            ["规则名称", "搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["无效A", "Python", "上海", "20-50K", "3-5年"],
            ["无效B", "Go", "杭州", "", "5-10年"],
            ["无效C", "", "北京", "50K以上", ""],
            ["无效D", "Java", "", "", ""],
        ]
        rules = module.parse_rules(rows)
        combinations = module.expand_rules(
            rules, max_combinations=64, city_resolver=fake_city_resolver
        )

        rule = rules[0]
        self.assertEqual(rule.search_terms, ("Python", "Go", "Java"))
        self.assertEqual(rule.cities, ("上海", "杭州", "北京"))
        self.assertEqual(rule.salary_codes, ("406", "407"))
        self.assertEqual(rule.experience_codes, ("105", "106"))
        self.assertEqual(len(combinations), 36)
        self.assertNotIn("无效A", json.dumps(module.plan_payload("rules.csv", rules, combinations), ensure_ascii=False))

    def test_missing_required_column_has_clear_error(self):
        rows = [["搜索关键词", "城市"], ["Python", "上海"]]
        with self.assertRaisesRegex(module.TableRuleError, "薪资待遇"):
            module.parse_rules(rows)

    def test_combination_cap_is_checked_before_search(self):
        rules = module.parse_rules(self.rows)
        with self.assertRaisesRegex(module.TableRuleError, "超过上限 4"):
            module.expand_rules(rules, max_combinations=4, city_resolver=fake_city_resolver)


class ExecutionTests(unittest.TestCase):
    def test_partial_detail_records_are_loaded_for_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "jobs.json"
            path.write_text(
                json.dumps([{"job_id": "done", "jd": "已完成"}], ensure_ascii=False),
                encoding="utf-8",
            )
            records = module.load_partial_detail_records(str(path))

        self.assertEqual(records, [{"job_id": "done", "jd": "已完成"}])

    def test_pages_default_means_all_pages(self):
        args = module.build_arg_parser().parse_args(["rules.csv"])
        self.assertIsNone(args.pages)

    def test_publish_date_range_is_inclusive_and_excludes_unknown_dates(self):
        jobs = [
            {"job_id": "before", "publish_date": "2026-07-31"},
            {"job_id": "start", "publish_date": "2026-08-01"},
            {"job_id": "end", "publish_date": "2026-08-31"},
            {"job_id": "after", "publish_date": "2026-09-01"},
            {"job_id": "unknown", "publish_date": ""},
        ]

        filtered, stats = module.filter_jobs_by_publish_date(
            jobs, date(2026, 8, 1), date(2026, 8, 31),
        )

        self.assertEqual([job["job_id"] for job in filtered], ["start", "end"])
        self.assertEqual(stats, {"input": 5, "matched": 2, "unknown": 1, "outside": 2})

    def test_publish_date_range_validation_rejects_bad_order_and_format(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            module.parse_publish_date_range("2026/08/01", "")
        with self.assertRaisesRegex(ValueError, "不能晚于"):
            module.parse_publish_date_range("2026-09-01", "2026-08-01")

    def test_dry_run_accepts_manual_pages_above_ten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            write_csv(table, [
                ["搜索关键词", "城市", "薪资待遇", "工作经验"],
                ["Python", "上海", "20-50K", "3-5年"],
            ])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = module.main([str(table), "--pages", "25", "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"combination_total": 1', output.getvalue())

    def test_duplicate_jobs_are_merged_and_keep_all_matching_conditions(self):
        combinations = [
            module.SearchCombination("Python", "上海", "101020100", "406", "105"),
            module.SearchCombination("Go", "上海", "101020100", "406", "105"),
        ]

        def fake_scrape(keyword, *_args, **_kwargs):
            return {
                "jobs": [
                    {"job_id": "same", "title": "Python Go后端工程师", "job_link": "https://example/same"},
                    {"job_id": keyword, "title": keyword},
                ]
            }

        jobs, runs = module.execute_plan(
            combinations, pages=1, cdp_port=9222, allow_dom_fallback=False,
            delay=0, scrape_func=fake_scrape,
        )

        self.assertEqual(len(jobs), 3)
        shared = next(job for job in jobs if job["job_id"] == "same")
        self.assertEqual(len(shared["matched_conditions"]), 2)
        self.assertEqual(len(runs), 2)

    def test_request_interval_is_forwarded_and_combination_wait_is_randomized(self):
        combinations = [
            module.SearchCombination("Python", "上海", "101020100", "406", "105"),
            module.SearchCombination("Go", "上海", "101020100", "406", "105"),
        ]
        request_intervals = []

        def fake_scrape(*_args, **kwargs):
            request_intervals.append(kwargs.get("request_interval"))
            return {"jobs": []}

        with mock.patch.object(
            module.boss, "randomized_request_interval", return_value=11.5
        ) as randomize, mock.patch.object(module.time, "sleep") as sleep:
            module.execute_plan(
                combinations, pages=1, cdp_port=9222, allow_dom_fallback=False,
                delay=2.5, scrape_func=fake_scrape,
            )

        self.assertEqual(request_intervals, [2.5, 2.5])
        randomize.assert_called_once_with(module.COMBINATION_INTERVAL_SECONDS)
        sleep.assert_called_once_with(11.5)

    def test_execute_plan_resumes_after_completed_combination(self):
        combinations = [
            module.SearchCombination("Python", "上海", "101020100", "406", "105"),
            module.SearchCombination("Go", "上海", "101020100", "406", "105"),
        ]
        initial_job = {
            "job_id": "python", "title": "Python", "matched_conditions": [
                combinations[0].to_dict()
            ],
        }
        initial_run = {**combinations[0].to_dict(), "jobs_found_raw": 1, "jobs_matched": 1}
        progress = []

        def fake_scrape(keyword, *_args, **_kwargs):
            self.assertEqual(keyword, "Go")
            return {"jobs": [{"job_id": "go", "title": "Go"}]}

        jobs, runs = module.execute_plan(
            combinations,
            pages=1,
            cdp_port=9222,
            allow_dom_fallback=False,
            delay=0,
            scrape_func=fake_scrape,
            start_index=1,
            initial_jobs=[initial_job],
            initial_runs=[initial_run],
            progress_callback=lambda saved_jobs, saved_runs, index: progress.append(
                (len(saved_jobs), len(saved_runs), index)
            ),
        )

        self.assertEqual({job["job_id"] for job in jobs}, {"python", "go"})
        self.assertEqual(len(runs), 2)
        self.assertEqual(progress, [(2, 2, 2)])

    def test_company_mode_filters_unrelated_search_hits(self):
        combinations = [
            module.SearchCombination(
                "字节跳动", "北京", "101010100", "406", "105", mode=module.MODE_COMPANY,
            )
        ]

        def fake_scrape(*_args, **_kwargs):
            return {"jobs": [
                {"job_id": "keep", "boss_name": "北京字节跳动科技有限公司"},
                {"job_id": "drop", "boss_name": "字节技术外包有限公司"},
            ]}

        jobs, runs = module.execute_plan(
            combinations, pages=1, cdp_port=9222, allow_dom_fallback=False,
            delay=0, scrape_func=fake_scrape,
        )

        self.assertEqual([job["job_id"] for job in jobs], ["keep"])
        self.assertEqual(runs[0]["jobs_found_raw"], 2)
        self.assertEqual(runs[0]["jobs_matched"], 1)

    def test_company_name_matching_is_normalized_and_configurable(self):
        self.assertTrue(module.company_name_matches("字节跳动", "北京字节跳动科技有限公司"))
        self.assertTrue(module.company_name_matches("腾讯有限公司", "腾讯", strategy="exact"))
        self.assertFalse(module.company_name_matches("腾讯", "腾讯云科技", strategy="exact"))
        self.assertFalse(module.company_name_matches("腾讯", "阿里巴巴"))

    def test_keyword_match_threshold_changes_fuzzy_job_filtering(self):
        combination = module.SearchCombination(
            "机器认", "上海", "101020100", "406", "105",
        )
        job = {"job_id": "one", "title": "机器人工程师"}

        self.assertTrue(module.job_matches_combination(
            combination, job, keyword_match_threshold=0.66,
        ))
        self.assertFalse(module.job_matches_combination(
            combination, job, keyword_match_threshold=0.9,
        ))

    def test_keyword_match_threshold_is_part_of_checkpoint_fingerprint(self):
        combinations = [
            module.SearchCombination("Python", "上海", "101020100", "406", "105"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table = pathlib.Path(temp_dir) / "rules.csv"
            table.write_text("rules", encoding="utf-8")
            low = module.search_checkpoint_fingerprint(
                str(table), combinations, 1, "contains", False, 0.66,
            )
            high = module.search_checkpoint_fingerprint(
                str(table), combinations, 1, "contains", False, 0.9,
            )

        self.assertNotEqual(low, high)

    def test_dry_run_does_not_require_runtime_dependencies_or_chrome(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "rules.csv"
            write_csv(path, rows)
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies") as dependencies, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = module.main([str(path), "--dry-run"])

        self.assertEqual(code, 0)
        dependencies.assert_not_called()
        self.assertIn('"within_cell": "OR"', output.getvalue())
        self.assertIn('"within_column": "OR"', output.getvalue())
        self.assertIn('"across_columns": "AND"', output.getvalue())
        self.assertIn('"row_alignment": "NONE"', output.getvalue())

    def test_main_skips_details_and_writes_default_csv_and_json_in_task_folder(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            output_root = pathlib.Path(temp_dir) / "result"
            write_csv(table_path, rows)
            jobs = [{
                "job_id": "abc", "title": "Python工程师", "boss_name": "示例公司",
                "location": "上海", "salary": "20-40K", "experience": "3-5年",
                "company_scale": "100-499人", "company_stage": "B轮",
                "company_industry": "互联网", "job_link": "https://example/abc",
            }]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_details") as scrape_details, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main([str(table_path), "--output-dir", str(output_root)])

            self.assertEqual(code, 0)
            scrape_details.assert_not_called()
            result_dirs = list(output_root.iterdir())
            self.assertEqual(len(result_dirs), 1)
            result_dir = result_dirs[0]
            with open(result_dir / "jobs.csv", encoding="utf-8-sig", newline="") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(exported, [{
                "job_id": "abc", "title": "Python工程师", "location": "上海",
                "salary": "20-40K", "experience": "3-5年",
                "company_scale": "100-499人", "company_stage": "B轮",
                "company_industry": "互联网",
            }])
            self.assertEqual(list(exported[0]), module.FINAL_CSV_COLUMNS)
            self.assertEqual(
                json.loads((result_dir / "jobs.json").read_text(encoding="utf-8")),
                exported,
            )
            self.assertTrue((result_dir / "metadata.json").is_file())

    def test_main_can_skip_jd_and_select_output_fields(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            result_dir = pathlib.Path(temp_dir) / "one-task"
            write_csv(table_path, rows)
            jobs = [{
                "job_id": "abc", "title": "Python工程师", "boss_name": "示例公司",
                "location": "上海", "salary": "20-40K", "experience": "3-5年",
            }]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_details") as scrape_details, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    str(table_path), "--result-dir", str(result_dir), "--no-detail",
                    "--output-fields", "title,salary,experience",
                ])

            self.assertEqual(code, 0)
            scrape_details.assert_not_called()
            with open(result_dir / "jobs.csv", encoding="utf-8-sig", newline="") as handle:
                rows_out = list(csv.DictReader(handle))
            self.assertEqual(rows_out, [{
                "title": "Python工程师", "salary": "20-40K", "experience": "3-5年",
            }])
            self.assertEqual(
                json.loads((result_dir / "jobs.json").read_text(encoding="utf-8")),
                rows_out,
            )

    def test_main_filters_by_publish_date_before_export(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            result_dir = pathlib.Path(temp_dir) / "one-task"
            write_csv(table_path, rows)
            jobs = [
                {"job_id": "keep", "title": "Python工程师", "boss_name": "公司A"},
                {"job_id": "drop", "title": "Go工程师", "boss_name": "公司B"},
            ]
            enriched = [
                {**jobs[0], "publish_date": "2026-08-15"},
                {**jobs[1], "publish_date": "2026-07-31"},
            ]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_publish_times", return_value=enriched) as scrape_times, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = module.main([
                    str(table_path), "--result-dir", str(result_dir), "--no-detail",
                    "--published-from", "2026-08-01", "--published-to", "2026-08-31",
                    "--output-fields", "job_id,publish_date", "--interval", "0",
                ])

            self.assertEqual(code, 0)
            scrape_times.assert_called_once_with(
                jobs,
                cdp_port=9222,
                request_interval=0.0,
                start_index=0,
                checkpoint_callback=mock.ANY,
            )
            self.assertIn("保留 1 条", output.getvalue())
            with open(result_dir / "jobs.csv", encoding="utf-8-sig", newline="") as handle:
                rows_out = list(csv.DictReader(handle))
            self.assertEqual(rows_out, [{"job_id": "keep", "publish_date": "2026-08-15"}])
            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(
                metadata["publish_filter_stats"],
                {"input": 2, "matched": 1, "unknown": 0, "outside": 1},
            )

    def test_main_can_fetch_publish_time_without_date_filter(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            result_dir = pathlib.Path(temp_dir) / "one-task"
            write_csv(table_path, rows)
            jobs = [{
                "job_id": "one", "title": "Python工程师", "boss_name": "公司A",
            }]
            enriched = [{**jobs[0], "publish_date": "2026-08-15"}]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_publish_times", return_value=enriched) as scrape_times, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    str(table_path), "--result-dir", str(result_dir), "--no-detail",
                    "--fetch-publish-time", "--output-fields", "job_id,publish_date",
                    "--interval", "0",
                ])

            self.assertEqual(code, 0)
            scrape_times.assert_called_once_with(
                jobs,
                cdp_port=9222,
                request_interval=0.0,
                start_index=0,
                checkpoint_callback=mock.ANY,
            )
            exported = json.loads((result_dir / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(exported, [{"job_id": "one", "publish_date": "2026-08-15"}])

    def test_main_can_disable_publish_time_lookup_with_date_filter(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            result_dir = pathlib.Path(temp_dir) / "one-task"
            write_csv(table_path, rows)
            jobs = [{
                "job_id": "one", "title": "Python工程师", "boss_name": "公司A",
                "publish_date": "2026-08-15",
            }]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_publish_times") as scrape_times, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    str(table_path), "--result-dir", str(result_dir), "--no-detail",
                    "--no-fetch-publish-time", "--published-from", "2026-08-01",
                    "--published-to", "2026-08-31", "--interval", "0",
                ])

            self.assertEqual(code, 0)
            scrape_times.assert_not_called()

    def test_main_reuses_candidate_checkpoint_after_detail_failure(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            table_path = root / "rules.csv"
            write_csv(table_path, rows)
            jobs = [{
                "job_id": "saved", "title": "Python工程师", "boss_name": "示例公司",
            }]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])) as execute, \
                    mock.patch.object(module.boss, "scrape_details", side_effect=RuntimeError("CDP reset")), \
                    contextlib.redirect_stdout(io.StringIO()):
                first_code = module.main([
                    str(table_path), "--result-dir", str(root / "task-1"),
                    "--fetch-detail",
                ])

            checkpoint_files = list((root / ".checkpoints").glob("*.json"))
            self.assertEqual(first_code, 1)
            self.assertEqual(len(checkpoint_files), 1)

            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan") as second_execute, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                second_code = module.main([
                    str(table_path), "--result-dir", str(root / "task-2"), "--no-detail",
                ])

            self.assertEqual(second_code, 0)
            second_execute.assert_not_called()
            self.assertIn("已加载检查点", output.getvalue())
            self.assertFalse((root / ".checkpoints").exists())
            exported = json.loads((root / "task-2" / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(exported[0]["job_id"], "saved")

    def test_main_resumes_publish_time_from_checkpoint_index(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["Python", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            table_path = root / "rules.csv"
            write_csv(table_path, rows)
            jobs = [
                {"job_id": "one", "title": "Python", "boss_name": "公司A"},
                {"job_id": "two", "title": "Python", "boss_name": "公司B"},
            ]

            def interrupt_publish(current_jobs, **kwargs):
                enriched = [
                    {**current_jobs[0], "publish_date": "2026-08-15"},
                    dict(current_jobs[1]),
                ]
                kwargs["checkpoint_callback"](enriched, 1)
                raise RuntimeError("interrupted")

            common_args = [
                str(table_path), "--no-detail", "--published-from", "2026-08-01",
                "--published-to", "2026-08-31", "--interval", "0",
            ]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_publish_times", side_effect=interrupt_publish), \
                    contextlib.redirect_stdout(io.StringIO()):
                first_code = module.main([
                    *common_args, "--result-dir", str(root / "task-1"),
                ])

            def finish_publish(current_jobs, **kwargs):
                self.assertEqual(kwargs["start_index"], 1)
                self.assertEqual(current_jobs[0]["publish_date"], "2026-08-15")
                return [
                    dict(current_jobs[0]),
                    {**current_jobs[1], "publish_date": "2026-08-20"},
                ]

            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan") as second_execute, \
                    mock.patch.object(module.boss, "scrape_publish_times", side_effect=finish_publish), \
                    contextlib.redirect_stdout(io.StringIO()):
                second_code = module.main([
                    *common_args, "--result-dir", str(root / "task-2"),
                ])

            self.assertEqual(first_code, 1)
            self.assertEqual(second_code, 0)
            second_execute.assert_not_called()
            exported = json.loads((root / "task-2" / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual([row["job_id"] for row in exported], ["one", "two"])

    def test_default_output_columns_include_salary_and_experience(self):
        self.assertEqual(
            module.FINAL_CSV_COLUMNS,
            [
                "job_id", "title", "location", "salary", "experience",
                "company_scale", "company_stage", "company_industry",
            ],
        )

    def test_relevant_jobs_are_aggregated_by_company(self):
        jobs = [
            {
                "title": "VLA 算法", "company": "示例科技",
                "encrypt_brand_id": "brand-1",
                "company_link": "https://www.zhipin.com/gongsi/brand-1.html",
            },
            {
                "title": "视觉算法", "company": "示例科技",
                "encrypt_brand_id": "brand-1",
                "company_link": "https://www.zhipin.com/gongsi/brand-1.html",
            },
        ]

        companies = module.aggregate_relevant_companies(jobs)

        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0]["matched_job_count"], 2)
        self.assertEqual(companies[0]["matched_job_titles"], ["VLA 算法", "视觉算法"])

    def test_company_csv_contains_exact_requested_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = pathlib.Path(temp_dir) / "companies.csv"
            module.write_company_csv(str(output_path), [
                {
                    "company_full_name": "北京示例科技有限公司",
                    "unified_social_credit_code": "91110108MA01234567",
                    "source_company_name": "示例科技",
                },
                {
                    "company_full_name": "",
                    "unified_social_credit_code": "",
                    "source_company_name": "未公开工商信息的品牌",
                },
            ])
            with open(output_path, encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(list(rows[0]), module.COMPANY_CSV_COLUMNS)
        self.assertEqual(rows, [{
            "company_full_name": "北京示例科技有限公司",
            "unified_social_credit_code": "91110108MA01234567",
        }])

    def test_company_registrations_are_deduplicated_by_credit_code(self):
        companies = module.deduplicate_company_registrations([
            {
                "company_full_name": "示例科技有限公司",
                "unified_social_credit_code": "91110108ma01234567",
                "source_company_name": "品牌A",
                "matched_job_count": 2,
                "matched_job_titles": ["算法A"],
            },
            {
                "company_full_name": "示例科技有限公司",
                "unified_social_credit_code": "91110108MA01234567",
                "source_company_name": "品牌B",
                "matched_job_count": 1,
                "matched_job_titles": ["算法B"],
            },
        ])

        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0]["unified_social_credit_code"], "91110108MA01234567")
        self.assertEqual(companies[0]["matched_job_count"], 3)
        self.assertEqual(companies[0]["matched_job_titles"], ["算法A", "算法B"])

    def test_main_llm_filters_jobs_and_writes_aggregated_company_csv(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["算法", "上海", "20-50K", "3-5年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            result_dir = pathlib.Path(temp_dir) / "task"
            write_csv(table_path, rows)
            jobs = [
                {
                    "job_id": "keep", "title": "VLA 算法", "boss_name": "公司A",
                    "company_link": "https://www.zhipin.com/gongsi/a.html",
                    "encrypt_brand_id": "a",
                },
                {
                    "job_id": "drop", "title": "销售", "boss_name": "公司B",
                    "company_link": "https://www.zhipin.com/gongsi/b.html",
                    "encrypt_brand_id": "b",
                },
            ]

            def fake_filter(records, requirements, batch_size):
                self.assertEqual(requirements, "需要 VLA 技术岗")
                self.assertEqual(batch_size, 20)
                return [{
                    **records[0],
                    "llm_match_reason": "VLA 匹配",
                }]

            registrations = [{
                "company_full_name": "上海公司A科技有限公司",
                "unified_social_credit_code": "91310101MA01234567",
            }]
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "execute_plan", return_value=(jobs, [])), \
                    mock.patch.object(module.boss, "scrape_details") as scrape_details, \
                    mock.patch.object(module.llm_filter, "validate_environment"), \
                    mock.patch.object(module.llm_filter, "filter_relevant_jobs", side_effect=fake_filter), \
                    mock.patch.object(
                        module.boss, "scrape_company_registrations",
                        return_value=registrations,
                    ) as scrape_companies, contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    str(table_path), "--result-dir", str(result_dir), "--no-detail",
                    "--job-requirements", "需要 VLA 技术岗", "--interval", "0",
                ])

            self.assertEqual(code, 0)
            scrape_details.assert_not_called()
            scrape_companies.assert_called_once()
            self.assertEqual(
                [row["job_id"] for row in json.loads(
                    (result_dir / "jobs.json").read_text(encoding="utf-8")
                )],
                ["keep"],
            )
            with open(result_dir / "companies.csv", encoding="utf-8-sig", newline="") as handle:
                companies = list(csv.DictReader(handle))
            self.assertEqual(companies, registrations)
            metadata = json.loads((result_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["llm_filter"]["candidate_jobs"], 2)
            self.assertEqual(metadata["llm_filter"]["relevant_jobs"], 1)

    def test_detail_limit_keeps_unfetched_jobs_with_blank_jd(self):
        jobs = [
            {"job_id": "a", "title": "A", "boss_name": "公司A", "experience": "1-3年"},
            {"job_id": "b", "title": "B", "boss_name": "公司B", "experience": "3-5年"},
        ]
        details = [{"job_id": "a", "title": "A", "company": "公司A", "jd": "岗位详情A"}]

        records = module.attach_match_metadata(details, jobs)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["jd"], "岗位详情A")
        self.assertEqual(records[1]["jd"], "")
        self.assertEqual(records[1]["experience"], "3-5年")

    def test_main_reports_cdp_connection_error_without_traceback(self):
        rows = [
            ["搜索关键词", "城市", "薪资待遇", "工作经验"],
            ["VLA", "北京", "20-50K", "1-3年"],
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            table_path = pathlib.Path(temp_dir) / "rules.csv"
            write_csv(table_path, rows)
            with mock.patch.object(module.boss, "resolve_city", side_effect=fake_city_resolver), \
                    mock.patch.object(module.boss, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(
                        module, "execute_plan",
                        side_effect=module.boss.CDPConnectionError("无法连接本机 Chrome CDP"),
                    ), contextlib.redirect_stdout(io.StringIO()) as output:
                code = module.main([str(table_path), "--delay", "0"])

        self.assertEqual(code, 1)
        self.assertIn("无法连接本机 Chrome CDP", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


class PackagingTests(unittest.TestCase):
    def test_batch_entrypoint_is_packaged(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertIn('boss-batch-search = "scripts.batch_search:main"', pyproject)
        self.assertNotIn("openpyxl", requirements)
        self.assertNotIn("pandas", requirements)


if __name__ == "__main__":
    unittest.main()
