import json
import unittest
from unittest import mock

from scripts import llm_filter as module


class LLMFilterTests(unittest.TestCase):
    def test_filters_in_batches_and_annotates_relevant_jobs(self):
        jobs = [
            {"job_id": "a", "title": "VLA 算法", "jd": "机器人多模态"},
            {"job_id": "b", "title": "销售", "jd": "客户开拓"},
            {"job_id": "c", "title": "视觉算法", "jd": "感知模型"},
        ]
        calls = []

        def fake_api(requirements, model_jobs):
            calls.append((requirements, model_jobs))
            rows = []
            for job in model_jobs:
                relevant = job["title"] != "销售"
                rows.append({
                    "id": job["id"],
                    "relevant": relevant,
                    "reason": "技术方向匹配" if relevant else "销售岗",
                })
            return {"output_text": json.dumps({"results": rows}, ensure_ascii=False)}

        result = module.filter_relevant_jobs(
            jobs, "机器人视觉和 VLA", batch_size=2, api_call=fake_api,
        )

        self.assertEqual([job["job_id"] for job in result], ["a", "c"])
        self.assertEqual(result[0]["llm_match_reason"], "技术方向匹配")
        self.assertEqual([len(call[1]) for call in calls], [2, 1])

    def test_rejects_missing_or_duplicate_decisions(self):
        jobs = [{"job_id": "a", "title": "A"}, {"job_id": "b", "title": "B"}]
        calls = []

        def incomplete(_requirements, _model_jobs):
            calls.append(1)
            return {"output_text": json.dumps({"results": [{
                "id": "0", "relevant": True, "reason": "ok",
            }]})}

        with mock.patch.object(module.time, "sleep"):
            with self.assertRaisesRegex(module.LLMFilterError, "缺少岗位 id"):
                module.filter_relevant_jobs(jobs, "需求", api_call=incomplete)

        self.assertEqual(len(calls), module.VALIDATION_RETRIES)

    def test_retries_incomplete_batch_then_succeeds(self):
        jobs = [
            {"job_id": "a", "title": "A"},
            {"job_id": "b", "title": "B"},
        ]
        calls = []

        def flaky(_requirements, model_jobs):
            calls.append(1)
            if len(calls) == 1:
                return {"output_text": json.dumps({"results": [
                    {"id": "0", "relevant": True, "reason": "ok"},
                ]})}
            rows = [
                {"id": job["id"], "relevant": True, "reason": "ok"}
                for job in model_jobs
            ]
            return {"output_text": json.dumps({"results": rows})}

        with mock.patch.object(module.time, "sleep"):
            result = module.filter_relevant_jobs(jobs, "需求", api_call=flaky)

        self.assertEqual([job["job_id"] for job in result], ["a", "b"])
        self.assertEqual(len(calls), 2)

    def test_reads_nested_responses_api_output_text(self):
        response = {"output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": '{"results":[]}'}],
        }]}
        self.assertEqual(module._response_output_text(response), '{"results":[]}')

    def test_empty_requirement_is_rejected(self):
        with self.assertRaisesRegex(module.LLMFilterError, "需求不能为空"):
            module.filter_relevant_jobs([], "  ")

    def test_missing_environment_is_rejected_before_api_use(self):
        with mock.patch.dict(module.os.environ, {}, clear=True):
            with self.assertRaisesRegex(module.LLMFilterError, "OPENAI_API_KEY"):
                module.validate_environment()

    def test_responses_api_uses_environment_and_strict_json_schema(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"output_text":"{\\"results\\":[]}"}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.dict(module.os.environ, {
            "OPENAI_API_KEY": "secret-test-key",
            "OPENAI_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://api.example.test/v1/",
            "OPENAI_TIMEOUT_SECONDS": "7",
        }, clear=True), mock.patch.object(module, "urlopen", side_effect=fake_urlopen):
            response = module._call_responses_api("需求", [])

        self.assertEqual(response["output_text"], '{"results":[]}')
        self.assertEqual(captured["url"], "https://api.example.test/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer secret-test-key")
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertFalse(captured["payload"]["store"])
        self.assertTrue(captured["payload"]["text"]["format"]["strict"])
        result_properties = captured["payload"]["text"]["format"]["schema"][
            "properties"
        ]["results"]["items"]["properties"]
        self.assertNotIn("score", result_properties)
        self.assertIn("不得使用关键词包含", captured["payload"]["instructions"])
        self.assertIn("合成灯光师", captured["payload"]["instructions"])
        self.assertIn("AI 算力", captured["payload"]["instructions"])
        self.assertEqual(captured["timeout"], 7.0)

    def test_gemini_uses_native_generate_content_api(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "candidates": [{
                        "content": {"parts": [{"text": '{"results":[]}'}]},
                    }],
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.dict(module.os.environ, {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-secret",
            "GEMINI_MODEL": "gemini-test-model",
            "GEMINI_BASE_URL": "https://generativelanguage.example/v1beta/",
            "LLM_TIMEOUT_SECONDS": "9",
        }, clear=True), mock.patch.object(module, "urlopen", side_effect=fake_urlopen):
            response = module._call_gemini_api("需求", [])

        self.assertEqual(response["output_text"], '{"results":[]}')
        self.assertEqual(
            captured["url"],
            "https://generativelanguage.example/v1beta/models/"
            "gemini-test-model:generateContent",
        )
        self.assertEqual(captured["headers"]["x-goog-api-key"], "gemini-secret")
        generation = captured["payload"]["generationConfig"]
        self.assertEqual(generation["responseMimeType"], "application/json")
        self.assertEqual(generation["responseJsonSchema"], module._response_schema())
        self.assertIn("语义分析员", captured["payload"]["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(captured["timeout"], 9.0)

    def test_gemini_key_auto_selects_gemini_provider(self):
        with mock.patch.dict(module.os.environ, {
            "GEMINI_API_KEY": "key", "GEMINI_MODEL": "model",
        }, clear=True):
            self.assertEqual(module.configured_provider(), "gemini")
            module.validate_environment()

    def test_deepseek_uses_responses_api_and_json_schema(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"output_text":"{\\"results\\":[]}"}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.dict(module.os.environ, {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "DEEPSEEK_MODEL": "deepseek-test-model",
            "DEEPSEEK_BASE_URL": "https://deepseek.example/v1/",
            "LLM_TIMEOUT_SECONDS": "11",
        }, clear=True), mock.patch.object(module, "urlopen", side_effect=fake_urlopen):
            response = module._call_deepseek_api("需求", [])

        self.assertEqual(response["output_text"], '{"results":[]}')
        self.assertEqual(captured["url"], "https://deepseek.example/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer deepseek-secret")
        self.assertEqual(captured["payload"]["model"], "deepseek-test-model")
        output_format = captured["payload"]["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertEqual(output_format["schema"], module._response_schema())
        self.assertEqual(captured["timeout"], 11.0)

    def test_deepseek_key_auto_selects_deepseek_provider(self):
        with mock.patch.dict(module.os.environ, {
            "DEEPSEEK_API_KEY": "key", "DEEPSEEK_MODEL": "model",
        }, clear=True):
            self.assertEqual(module.configured_provider(), "deepseek")
            self.assertEqual(module.configured_model_name(), "model")
            module.validate_environment()

    def test_siliconflow_uses_chat_completions_and_json_schema(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{
                        "message": {"content": '{"results":[]}'},
                    }],
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.dict(module.os.environ, {
            "LLM_PROVIDER": "siliconflow",
            "SILICONFLOW_API_KEY": "siliconflow-secret",
            "SILICONFLOW_MODEL": "deepseek-ai/DeepSeek-Test",
            "SILICONFLOW_BASE_URL": "https://siliconflow.example/v1/",
            "LLM_TIMEOUT_SECONDS": "13",
        }, clear=True), mock.patch.object(module, "urlopen", side_effect=fake_urlopen):
            response = module._call_siliconflow_api("需求", [])

        self.assertEqual(response["output_text"], '{"results":[]}')
        self.assertEqual(
            captured["url"], "https://siliconflow.example/v1/chat/completions",
        )
        self.assertEqual(captured["authorization"], "Bearer siliconflow-secret")
        self.assertEqual(
            captured["payload"]["model"], "deepseek-ai/DeepSeek-Test",
        )
        response_format = captured["payload"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(
            response_format["json_schema"]["schema"], module._response_schema(),
        )
        self.assertEqual(captured["timeout"], 13.0)

    def test_siliconflow_key_auto_selects_siliconflow_provider(self):
        with mock.patch.dict(module.os.environ, {
            "SILICONFLOW_API_KEY": "key", "SILICONFLOW_MODEL": "model",
        }, clear=True):
            self.assertEqual(module.configured_provider(), "siliconflow")
            self.assertEqual(module.configured_model_name(), "model")
            module.validate_environment()


if __name__ == "__main__":
    unittest.main()
