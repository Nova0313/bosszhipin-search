#!/usr/bin/env python3
"""Read keyword/company rules from CSV/TSV/XLSX and execute BOSS searches.

Boolean semantics are deliberately explicit:

* tags inside one cell are OR alternatives;
* all non-empty values in the same column are OR alternatives;
* search-term/city/salary/experience columns are AND constraints;
* rows have no positional relationship and may have different lengths;
* results are merged by job_id (with a stable fallback key).

No model, embedding, semantic classifier, or model API is used.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import posixpath
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree as ET

try:
    from . import boss_cdp_raw as boss
except ImportError:  # Direct execution: python scripts/batch_search.py ...
    import boss_cdp_raw as boss


__version__ = boss.__version__

MODE_KEYWORD = "keyword"
MODE_COMPANY = "company"
MODE_LABELS = {MODE_KEYWORD: "关键词", MODE_COMPANY: "公司"}
COMMON_REQUIRED_COLUMNS = ("city", "salary", "experience")
HEADER_ALIASES = {
    "keyword": {"搜索关键词", "关键词", "职位关键词", "keyword", "keywords", "query"},
    "company": {"公司名称", "目标公司", "公司", "company", "employer", "brand"},
    "city": {"城市", "工作城市", "工作地点", "city", "location"},
    "salary": {"薪资待遇", "薪资", "薪资范围", "salary"},
    "experience": {"工作经验", "经验", "经验要求", "experience"},
}

TAG_SPLIT_RE = re.compile(r"[\r\n,，;；|、]+")
CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# Friendly aliases that expand to one or more BOSS filter codes.
SALARY_ALIASES = {
    "不限": [""],
    "无限制": [""],
    "任意": [""],
    "10k以上": ["405", "406", "407"],
    "10k+": ["405", "406", "407"],
    "20k以上": ["406", "407"],
    "20k+": ["406", "407"],
    "50k以上": ["407"],
    "50k+": ["407"],
}
EXPERIENCE_ALIASES = {
    "不限": [""],
    "无限制": [""],
    "任意": [""],
    "无经验": ["101"],
    "3年以上": ["105", "106", "107"],
    "3年+": ["105", "106", "107"],
    "5年以上": ["106", "107"],
    "5年+": ["106", "107"],
    "10年以上": ["107"],
    "10年+": ["107"],
}


class TableRuleError(ValueError):
    """The search-rules table is malformed or contains unsupported labels."""


@dataclass(frozen=True)
class SearchRule:
    mode: str
    search_terms: tuple[str, ...]
    cities: tuple[str, ...]
    salary_codes: tuple[str, ...]
    experience_codes: tuple[str, ...]
    salary_labels: tuple[str, ...]
    experience_labels: tuple[str, ...]

    @property
    def combination_count(self) -> int:
        return (
            len(self.search_terms)
            * len(self.cities)
            * len(self.salary_codes)
            * len(self.experience_codes)
        )


@dataclass
class SearchCombination:
    search_term: str
    city_name: str
    city_code: str
    salary_code: str
    experience_code: str
    mode: str = MODE_KEYWORD

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.search_term, self.city_code, self.salary_code, self.experience_code)

    def filters(self) -> dict[str, str]:
        result = {}
        if self.salary_code:
            result["salary"] = self.salary_code
        if self.experience_code:
            result["experience"] = self.experience_code
        return result

    def to_dict(self) -> dict:
        result = {
            "mode": self.mode,
            "city": self.city_name,
            "city_code": self.city_code,
            "salary": display_filter_code(self.salary_code, boss.SALARY_MAP),
            "salary_code": self.salary_code or "0",
            "experience": display_filter_code(self.experience_code, boss.EXPERIENCE_MAP),
            "experience_code": self.experience_code or "0",
        }
        result[self.mode] = self.search_term
        return result


def normalize_label(value) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    return text.replace("－", "-").replace("—", "-").replace("–", "-")


def split_tags(value) -> list[str]:
    """Split a cell into stable, de-duplicated OR alternatives."""
    if value is None:
        return []
    result = []
    seen = set()
    for part in TAG_SPLIT_RE.split(str(value)):
        tag = part.strip()
        key = normalize_label(tag)
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
    return result


def canonical_header(value) -> str | None:
    normalized = normalize_label(value).replace("_", "")
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in {normalize_label(alias).replace("_", "") for alias in aliases}:
            return canonical
    return None


def _decode_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise TableRuleError(f"无法识别表格编码: {path}")


def read_delimited_table(path: Path) -> list[list[str]]:
    text = _decode_text_file(path)
    default_delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = default_delimiter
    return [list(row) for row in csv.reader(text.splitlines(), delimiter=delimiter)]


def _column_index(cell_ref: str) -> int:
    match = CELL_REF_RE.match(cell_ref)
    if not match:
        raise TableRuleError(f"无效的 Excel 单元格坐标: {cell_ref}")
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"x": XLSX_MAIN_NS}
    return ["".join(node.text or "" for node in item.findall(".//x:t", ns))
            for item in root.findall("x:si", ns)]


def _xlsx_sheet_path(archive: zipfile.ZipFile, sheet_name: str | None) -> tuple[str, str]:
    ns = {"x": XLSX_MAIN_NS, "r": XLSX_REL_NS}
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets = workbook.findall("x:sheets/x:sheet", ns)
    available = [sheet.attrib.get("name", "") for sheet in sheets]
    selected = None
    if sheet_name:
        selected = next((sheet for sheet in sheets if sheet.attrib.get("name") == sheet_name), None)
        if selected is None:
            raise TableRuleError(
                f"Excel 中不存在工作表 {sheet_name!r}，可选: {', '.join(available)}"
            )
    elif sheets:
        selected = sheets[0]
    if selected is None:
        raise TableRuleError("Excel 中没有可读取的工作表")

    relation_id = selected.attrib.get(f"{{{XLSX_REL_NS}}}id")
    rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = None
    for relation in rel_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
        if relation.attrib.get("Id") == relation_id:
            target = relation.attrib.get("Target")
            break
    if not target:
        raise TableRuleError(f"无法解析 Excel 工作表: {selected.attrib.get('name', '')}")
    path = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
    if path.startswith("../"):
        raise TableRuleError("Excel 工作表路径越界")
    return selected.attrib.get("name", ""), path


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{XLSX_MAIN_NS}}}t"))
    value_node = cell.find(f"{{{XLSX_MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text
    if cell_type == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            raise TableRuleError(f"Excel shared string 索引无效: {value}")
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def read_xlsx_table(path: Path, sheet_name: str | None = None) -> list[list[str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            shared = _shared_strings(archive)
            _, worksheet_path = _xlsx_sheet_path(archive, sheet_name)
            root = ET.fromstring(archive.read(worksheet_path))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise TableRuleError(f"无法读取 Excel 文件 {path}: {exc}") from exc

    rows = []
    for row in root.findall(f".//{{{XLSX_MAIN_NS}}}sheetData/{{{XLSX_MAIN_NS}}}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{{{XLSX_MAIN_NS}}}c"):
            ref = cell.attrib.get("r", "")
            values[_column_index(ref)] = _xlsx_cell_value(cell, shared)
        if values:
            rows.append([values.get(index, "") for index in range(max(values) + 1)])
        else:
            rows.append([])
    return rows


def read_table(path: str, sheet_name: str | None = None) -> list[list[str]]:
    table_path = Path(path).expanduser().resolve()
    if not table_path.is_file():
        raise TableRuleError(f"表格文件不存在: {table_path}")
    suffix = table_path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        return read_delimited_table(table_path)
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx_table(table_path, sheet_name)
    if suffix == ".xls":
        raise TableRuleError("不支持旧版 .xls，请另存为 .xlsx 或 .csv")
    raise TableRuleError("仅支持 .csv、.tsv、.xlsx 和 .xlsm 表格")


def _mapping_lookup(mapping: dict[str, str]) -> dict[str, str]:
    result = {normalize_label(label): code for label, code in mapping.items()}
    result.update({str(code): str(code) for code in mapping.values()})
    return result


def resolve_filter_labels(
    labels: Iterable[str],
    mapping: dict[str, str],
    aliases: dict[str, list[str]],
    column_name: str,
) -> tuple[str, ...]:
    labels = list(labels) or ["不限"]
    lookup = _mapping_lookup(mapping)
    result = []
    for label in labels:
        normalized = normalize_label(label)
        if normalized in aliases:
            codes = aliases[normalized]
        elif normalized in lookup:
            code = lookup[normalized]
            codes = ["" if code == "0" else code]
        else:
            supported = "、".join(mapping.keys())
            raise TableRuleError(
                f"{column_name}标签 {label!r} 不受支持；可用值: {supported}"
            )
        for code in codes:
            if code not in result:
                result.append(code)
    return tuple(result)


def parse_rules(rows: list[list[str]], mode: str = MODE_KEYWORD) -> list[SearchRule]:
    if mode not in MODE_LABELS:
        raise TableRuleError(f"不支持的搜索模式: {mode}")
    nonempty = [(index + 1, row) for index, row in enumerate(rows) if any(str(v).strip() for v in row)]
    if not nonempty:
        raise TableRuleError("表格为空")
    header_row_number, header = nonempty[0]
    columns: dict[str, int] = {}
    for index, value in enumerate(header):
        canonical = canonical_header(value)
        if not canonical:
            continue
        if canonical in columns:
            raise TableRuleError(f"表头重复: {value}")
        columns[canonical] = index
    required_columns = (mode, *COMMON_REQUIRED_COLUMNS)
    missing = [name for name in required_columns if name not in columns]
    if missing:
        display = {
            "keyword": "搜索关键词", "company": "公司名称", "city": "城市",
            "salary": "薪资待遇", "experience": "工作经验",
        }
        raise TableRuleError("表格缺少必需列: " + "、".join(display[name] for name in missing))

    data_rows = nonempty[1:]
    if not data_rows:
        raise TableRuleError(f"表头在第 {header_row_number} 行，但没有找到数据行")

    def collect_column_tags(name: str) -> list[str]:
        """Collect one independent OR pool from every non-empty cell in a column."""
        column_index = columns[name]
        values = []
        seen = set()
        for _, row in data_rows:
            cell_value = row[column_index] if column_index < len(row) else ""
            for tag in split_tags(cell_value):
                normalized = normalize_label(tag)
                if normalized not in seen:
                    values.append(tag)
                    seen.add(normalized)
        return values

    search_terms = collect_column_tags(mode)
    cities = collect_column_tags("city")
    salary_labels = collect_column_tags("salary") or ["不限"]
    experience_labels = collect_column_tags("experience") or ["不限"]
    if not search_terms:
        label = "搜索关键词" if mode == MODE_KEYWORD else "公司名称"
        raise TableRuleError(f"{label}列没有任何有效值")
    if not cities:
        raise TableRuleError("城市列没有任何有效值；如需全国请填写“全国”")

    return [SearchRule(
        mode=mode,
        search_terms=tuple(search_terms),
        cities=tuple(cities),
        salary_codes=resolve_filter_labels(
            salary_labels, boss.SALARY_MAP, SALARY_ALIASES, "薪资待遇"
        ),
        experience_codes=resolve_filter_labels(
            experience_labels, boss.EXPERIENCE_MAP, EXPERIENCE_ALIASES, "工作经验"
        ),
        salary_labels=tuple(salary_labels),
        experience_labels=tuple(experience_labels),
    )]


def display_filter_code(code: str, mapping: dict[str, str]) -> str:
    if not code:
        return "不限"
    return next((label for label, value in mapping.items() if value == code), code)


def expand_rules(
    rules: Iterable[SearchRule],
    max_combinations: int = 64,
    city_resolver: Callable[[str], tuple[str, str]] | None = None,
) -> list[SearchCombination]:
    if max_combinations <= 0:
        raise TableRuleError("--max-combinations 必须大于 0")
    city_resolver = city_resolver or boss.resolve_city
    combinations: dict[tuple[str, str, str, str], SearchCombination] = {}
    raw_total = 0
    for rule in rules:
        raw_total += rule.combination_count
        if raw_total > max_combinations:
            raise TableRuleError(
                f"表格会展开至少 {raw_total} 个搜索组合，超过上限 {max_combinations}。"
                "请减少单元格标签，或显式调大 --max-combinations。"
            )
        resolved_cities = [city_resolver(city) for city in rule.cities]
        for search_term, (city_name, city_code), salary, experience in itertools.product(
            rule.search_terms, resolved_cities, rule.salary_codes, rule.experience_codes
        ):
            candidate = SearchCombination(
                search_term=search_term,
                city_name=city_name,
                city_code=city_code,
                salary_code=salary,
                experience_code=experience,
                mode=rule.mode,
            )
            existing = combinations.get(candidate.key)
            if existing is None:
                combinations[candidate.key] = candidate
    return list(combinations.values())


def build_plan(
    table_path: str,
    mode: str = MODE_KEYWORD,
    sheet_name: str | None = None,
    max_combinations: int = 64,
    city_resolver: Callable[[str], tuple[str, str]] | None = None,
) -> tuple[list[SearchRule], list[SearchCombination]]:
    rules = parse_rules(read_table(table_path, sheet_name), mode=mode)
    combinations = expand_rules(rules, max_combinations, city_resolver)
    return rules, combinations


def _job_key(job: dict) -> str:
    return str(
        job.get("job_id")
        or job.get("job_link")
        or f"{job.get('title', '')}|{job.get('boss_name', '')}|{job.get('location', '')}"
    )


COMPANY_PUNCTUATION_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)
COMPANY_LEGAL_SUFFIX_RE = re.compile(
    r"(?:集团有限责任公司|集团股份有限公司|集团有限公司|有限责任公司|股份有限公司|有限公司|公司)$"
)


def normalize_company_name(value: str, strip_legal_suffix: bool = False) -> str:
    """Normalize company names with deterministic text rules only."""
    normalized = COMPANY_PUNCTUATION_RE.sub("", str(value or "").lower())
    if strip_legal_suffix:
        normalized = COMPANY_LEGAL_SUFFIX_RE.sub("", normalized)
    return normalized


def company_name_matches(expected: str, actual: str, strategy: str = "contains") -> bool:
    """Validate that a search hit belongs to the requested company."""
    expected_full = normalize_company_name(expected)
    actual_full = normalize_company_name(actual)
    if not expected_full or not actual_full:
        return False
    expected_base = normalize_company_name(expected, strip_legal_suffix=True)
    actual_base = normalize_company_name(actual, strip_legal_suffix=True)
    if strategy == "exact":
        return expected_full == actual_full or expected_base == actual_base
    if strategy != "contains":
        raise ValueError(f"unsupported company matching strategy: {strategy}")
    return (
        expected_full in actual_full
        or actual_full in expected_full
        or expected_base in actual_base
        or actual_base in expected_base
    )


def execute_plan(
    combinations: list[SearchCombination],
    pages: int | None,
    cdp_port: int,
    allow_dom_fallback: bool,
    delay: float,
    company_match: str = "contains",
    scrape_func: Callable | None = None,
) -> tuple[list[dict], list[dict]]:
    scrape_func = scrape_func or boss.scrape_list
    jobs_by_key: dict[str, dict] = {}
    runs = []
    with tempfile.TemporaryDirectory(prefix="boss_batch_") as temp_dir:
        for index, combination in enumerate(combinations, 1):
            condition = combination.to_dict()
            print(
                f"\n=== 组合 {index}/{len(combinations)}: "
                f"{combination.search_term} AND {condition['city']} AND "
                f"{condition['salary']} AND {condition['experience']} ==="
            )
            intermediate = os.path.join(temp_dir, f"run_{index:04d}.json")
            data = scrape_func(
                combination.search_term,
                combination.city_code,
                pages,
                combination.filters(),
                intermediate,
                cdp_port=cdp_port,
                fmt="json",
                allow_dom_fallback=allow_dom_fallback,
                request_interval=delay,
            )
            raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
            if combination.mode == MODE_COMPANY:
                found_jobs = [
                    job for job in raw_jobs
                    if company_name_matches(
                        combination.search_term,
                        job.get("boss_name") or job.get("company") or "",
                        company_match,
                    )
                ]
            else:
                found_jobs = raw_jobs
            runs.append({
                **condition,
                "jobs_found_raw": len(raw_jobs),
                "jobs_matched": len(found_jobs),
            })
            for raw_job in found_jobs:
                job = dict(raw_job)
                key = _job_key(job)
                existing = jobs_by_key.get(key)
                if existing is None:
                    job["matched_conditions"] = [condition]
                    jobs_by_key[key] = job
                elif condition not in existing.setdefault("matched_conditions", []):
                    existing["matched_conditions"].append(condition)
            if index < len(combinations) and delay > 0:
                wait_seconds = delay
                print(f"组合间等待 {wait_seconds:.1f}s，降低请求密度...")
                time.sleep(wait_seconds)
    return list(jobs_by_key.values()), runs


def _rule_to_dict(rule: SearchRule) -> dict:
    value = asdict(rule)
    value["combination_count"] = rule.combination_count
    return value


def plan_payload(table_path: str, rules: list[SearchRule], combinations: list[SearchCombination]) -> dict:
    return {
        "source_table": str(Path(table_path).expanduser().resolve()),
        "mode": rules[0].mode if rules else MODE_KEYWORD,
        "logic": {
            "within_cell": "OR",
            "within_column": "OR",
            "across_columns": "AND",
            "row_alignment": "NONE",
        },
        "rules": [_rule_to_dict(rule) for rule in rules],
        "combination_total": len(combinations),
        "combinations": [combination.to_dict() for combination in combinations],
    }


OUTPUT_FIELD_LABELS = {
    "job_id": "Job ID",
    "title": "职位名称",
    "company": "公司",
    "location": "地点",
    "salary": "薪资",
    "experience": "工作经验",
    "jd": "JD 内容",
    "job_link": "职位链接",
    "boss_active_status": "招聘者活跃状态",
    "company_scale": "公司规模",
    "company_stage": "融资阶段",
    "company_industry": "公司行业",
    "skills": "技能标签",
    "welfare": "福利",
}
FINAL_CSV_COLUMNS = [
    "job_id", "title", "company", "location", "salary", "experience", "jd",
]
DEFAULT_RESULT_ROOT = Path(__file__).resolve().parents[1] / "result"


def parse_output_fields(raw_fields: str | None) -> list[str]:
    if raw_fields is None:
        return list(FINAL_CSV_COLUMNS)
    fields = []
    for field_name in str(raw_fields).split(","):
        field_name = field_name.strip()
        if field_name and field_name not in fields:
            fields.append(field_name)
    if not fields:
        raise ValueError("输出字段不能为空")
    unknown = [field_name for field_name in fields if field_name not in OUTPUT_FIELD_LABELS]
    if unknown:
        raise ValueError(f"不支持的输出字段: {', '.join(unknown)}")
    return fields


def create_result_directory(
    output_root: str | None,
    table_path: str,
    mode: str,
    exact_result_dir: str | None = None,
) -> Path:
    """Create one isolated folder for a batch run."""
    if exact_result_dir:
        result_dir = Path(exact_result_dir).expanduser().resolve()
        result_dir.mkdir(parents=True, exist_ok=True)
        return result_dir

    root = Path(output_root).expanduser().resolve() if output_root else DEFAULT_RESULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    table_stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", Path(table_path).stem).strip("_")
    table_stem = table_stem or "rules"
    result_dir = root / f"{table_stem}_{mode}_{timestamp}"
    result_dir.mkdir(exist_ok=False)
    return result_dir


def output_paths(result_dir: Path) -> tuple[str, str, str]:
    return (
        str(result_dir / "metadata.json"),
        str(result_dir / "jobs.csv"),
        str(result_dir / "jobs.json"),
    )


def write_final_csv(csv_path: str, records: list[dict], fields: list[str] | None = None) -> None:
    """Write selected job fields to the final CSV."""
    fields = fields or list(FINAL_CSV_COLUMNS)
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in fields})
    print(f"结果 CSV 已保存: {csv_path}")


def write_final_json(json_path: str, records: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or list(FINAL_CSV_COLUMNS)
    projected = [
        {field_name: record.get(field_name, "") for field_name in fields}
        for record in records
    ]
    boss._atomic_write_json(json_path, projected)
    print(f"结果 JSON 已保存: {json_path}")


def _experience_from_record(record: dict) -> str:
    if record.get("experience"):
        return str(record["experience"])
    tags = str(record.get("tags_list") or record.get("tags") or "")
    for tag in re.split(r"\s*\|\s*", tags):
        if "经验" in tag or "年" in tag or tag in {"应届生", "在校生"}:
            return tag
    return ""


def export_record_from_job(job: dict) -> dict:
    record = dict(job)
    record["company"] = job.get("company") or job.get("boss_name") or ""
    record["experience"] = _experience_from_record(job)
    record.setdefault("jd", "")
    return record


def attach_match_metadata(details: list[dict], jobs: list[dict]) -> list[dict]:
    """Merge fetched details into every list job, preserving jobs without JD."""
    details_by_key = {_job_key(detail): detail for detail in details}
    result = []
    for job in jobs:
        detail = export_record_from_job(job)
        fetched_detail = details_by_key.get(_job_key(job))
        if fetched_detail:
            detail.update(fetched_detail)
            detail["experience"] = _experience_from_record(detail)
        if job and job.get("matched_conditions"):
            detail["matched_conditions"] = job["matched_conditions"]
        result.append(detail)
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"BOSS 表格组合搜索 v{__version__}（单元格 OR，列间 AND）"
    )
    parser.add_argument("table", help="规则表格：.csv / .tsv / .xlsx / .xlsm")
    parser.add_argument("--mode", choices=[MODE_KEYWORD, MODE_COMPANY], default=MODE_KEYWORD,
                        help="搜索模式：keyword（默认）或 company")
    parser.add_argument("--sheet", default=None, help="Excel 工作表名（默认第一个）")
    parser.add_argument("--pages", type=int, default=None,
                        help="每个搜索组合的页数上限（默认全部；手动设置正整数）")
    parser.add_argument("--max-combinations", type=int, default=64,
                        help="表格最大展开组合数（默认 64）")
    parser.add_argument("--interval", "--delay", dest="interval", type=float, default=8.0,
                        help="组合、翻页和岗位详情之间的等待秒数（默认 8）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验表格并展示组合，不连接 Chrome")
    parser.add_argument("--output-dir", "--output", dest="output_dir", default=None,
                        help="结果根目录；每次任务会在其中新建文件夹并生成 CSV/JSON")
    parser.add_argument("--result-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cdp-port", type=int, default=boss.DEFAULT_CDP_PORT)
    parser.add_argument("--allow-dom-fallback", action="store_true")
    parser.add_argument("--max-details", type=int, default=None,
                        help="最多抓取多少个详情（默认全部）")
    parser.add_argument("--no-detail", dest="fetch_jd", action="store_false", default=True,
                        help="不抓取 JD，直接输出职位列表")
    parser.add_argument("--output-fields", default=None,
                        help=f"逗号分隔的输出字段（默认 {','.join(FINAL_CSV_COLUMNS)}）")
    parser.add_argument("--company-match", choices=["contains", "exact"], default="contains",
                        help="公司模式的公司名校验策略（默认 contains）")
    parser.add_argument("--analysis", action="store_true", help="输出固定规则聚合分析")
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.pages is not None and args.pages <= 0:
        print("❌ --pages 必须是正整数")
        return 2
    if args.interval < 0:
        print("❌ --interval 不能小于 0")
        return 2
    if args.max_details is not None and args.max_details <= 0:
        print("❌ --max-details 必须大于 0")
        return 2
    try:
        output_fields = parse_output_fields(args.output_fields)
        rules, combinations = build_plan(
            args.table,
            mode=args.mode,
            sheet_name=args.sheet,
            max_combinations=args.max_combinations,
        )
    except (TableRuleError, boss.CityResolutionError, ValueError) as exc:
        print(f"❌ 表格规则无效: {exc}")
        return 2

    estimated_requests = len(combinations) * args.pages if args.pages is not None else None
    if estimated_requests is not None and estimated_requests > boss.MAX_API_REQUESTS:
        print(
            f"❌ 预计列表请求 {estimated_requests} 次，"
            f"超过单次上限 {boss.MAX_API_REQUESTS}"
        )
        return 2
    if args.pages is None and len(combinations) > boss.MAX_API_REQUESTS:
        print(
            f"❌ 全部页模式至少需要 {len(combinations)} 次列表请求，"
            f"超过单次上限 {boss.MAX_API_REQUESTS}"
        )
        return 2

    payload = plan_payload(args.table, rules, combinations)
    rule = rules[0]
    print(
        f"已汇总列值: {MODE_LABELS[rule.mode]} {len(rule.search_terms)} 个，"
        f"城市 {len(rule.cities)} 个，薪资 {len(rule.salary_codes)} 个，"
        f"经验 {len(rule.experience_codes)} 个；"
        f"展开为 {len(combinations)} 个唯一搜索组合\n"
        "逻辑: 同列所有值 OR，列之间 AND，行之间没有对应关系"
    )
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not boss.require_runtime_dependencies("requests", "websocket"):
        return 1

    try:
        jobs, runs = execute_plan(
            combinations,
            pages=args.pages,
            cdp_port=args.cdp_port,
            allow_dom_fallback=args.allow_dom_fallback,
            delay=args.interval,
            company_match=args.company_match,
        )
    except (boss.LoginGateError, boss.CDPConnectionError) as exc:
        print(str(exc))
        return 1

    payload.update({
        "scraped_at": datetime.now().isoformat(),
        "runs": runs,
        "total": len(jobs),
        "jobs": jobs,
        "options": {
            "pages": args.pages,
            "interval": args.interval,
            "fetch_jd": args.fetch_jd,
            "max_details": args.max_details,
            "company_match": args.company_match,
            "output_fields": output_fields,
        },
    })
    try:
        result_dir = create_result_directory(
            args.output_dir, args.table, args.mode, exact_result_dir=args.result_dir,
        )
    except OSError as exc:
        print(f"❌ 无法创建结果目录: {exc}")
        return 1
    metadata_path, csv_path, json_path = output_paths(result_dir)
    boss._atomic_write_json(metadata_path, payload)
    print(f"\n合并去重后共 {len(jobs)} 条职位")
    print(f"检索元数据已保存: {metadata_path}")

    list_data = {
        "keyword": "表格组合搜索",
        "city": "多城市",
        "total": len(jobs),
        "jobs": jobs,
    }
    records = []
    if jobs and args.fetch_jd:
        try:
            details = boss.scrape_details(
                list_data,
                max_details=args.max_details,
                output_path=json_path,
                cdp_port=args.cdp_port,
                fmt="json",
                request_interval=args.interval,
            )
        except RuntimeError as exc:
            print(f"❌ 详情抓取中止: {exc}")
            return 1
        records = attach_match_metadata(details, jobs)
    elif jobs:
        records = [export_record_from_job(job) for job in jobs]
        print("已关闭 JD 抓取，直接输出职位列表")
    write_final_json(json_path, records, output_fields)
    write_final_csv(csv_path, records, output_fields)
    print(f"结果目录: {result_dir}")
    print(f"最终输出 {len(records)} 条岗位记录")
    if args.analysis:
        boss.analyze(list_data, records, search_keyword="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
