import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("requests", mock.Mock())
    sys.modules.setdefault("websocket", mock.Mock())
    spec = importlib.util.spec_from_file_location("new_boss_scraper", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProjectShapeTests(unittest.TestCase):
    def test_project_has_no_llm_dependency(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        combined = pyproject + requirements
        for dependency in ("openai", "anthropic", "langchain", "llamaindex", "gemini"):
            self.assertNotIn(dependency, combined)

    def test_version_is_consistent(self):
        module = load_module()
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(module.__version__, "1.4.1")
        self.assertIn('version = "1.4.1"', pyproject)

    def test_default_paths_are_isolated(self):
        module = load_module()
        self.assertIn(".boss-zhipin-python", module.DEFAULT_CDP_DATA_DIR)
        self.assertIn(".boss-zhipin-python", module.DEFAULT_RESULT_DIR)
        self.assertNotEqual(module.DEFAULT_CDP_DATA_DIR, module.DEFAULT_PROFILE_DIR)


class CityTests(unittest.TestCase):
    def test_city_map_loads_from_new_project(self):
        module = load_module()
        module._local_city_map_cache = None
        by_name, by_code = module.load_local_city_map()
        self.assertGreater(len(by_name), 100)
        self.assertEqual(by_name["上海"], "101020100")
        self.assertEqual(by_code["101020100"], "上海")

    def test_resolve_city_supports_name_and_code(self):
        module = load_module()
        self.assertEqual(module.resolve_city("上海"), ("上海", "101020100"))
        self.assertEqual(module.resolve_city("101020100"), ("上海", "101020100"))


class OutputTests(unittest.TestCase):
    def test_default_output_path_uses_persistent_result_dir(self):
        module = load_module()
        output = module.default_output_path("jobs")
        self.assertTrue(output.startswith(module.DEFAULT_RESULT_DIR))
        self.assertIn("boss_jobs_", output)
        self.assertTrue(output.endswith(".json"))

    def test_incremental_json_write_round_trip(self):
        module = load_module()
        payload = {
            "keyword": "Python",
            "city": "上海",
            "jobs": [{"job_id": "1", "title": "Python 开发"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "jobs.json"
            module._atomic_write_json(str(path), payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
