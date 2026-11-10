import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from src.config import Settings
from src.model_client import ModelError, complete_json
from src.news import NewsError, parse_results, search_news


def response(payload):
    handle = MagicMock()
    handle.__enter__.return_value = handle
    handle.read.return_value = json.dumps(payload).encode()
    return handle


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_default_is_offline(self):
        self.assertEqual(Settings.from_env().mode, "offline")

    @patch.dict(os.environ, {"APP_MODE": "api"}, clear=True)
    def test_api_mode_requires_model_settings(self):
        with self.assertRaises(ValueError):
            Settings.from_env()

    def test_fallback_needs_url_and_key_over_https(self):
        base = {"APP_MODE": "api", "LLM_BASE_URL": "https://a.example", "LLM_MODEL": "m", "LLM_API_KEY": "k"}
        with patch.dict(os.environ, {**base, "LLM_FALLBACK_API_KEY": "b"}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {**base, "LLM_FALLBACK_API_KEY": "b", "LLM_FALLBACK_BASE_URL": "http://b.example"}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict(os.environ, {**base, "LLM_FALLBACK_API_KEY": "backup-secret", "LLM_FALLBACK_BASE_URL": "https://b.example/v1/"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.fallback_base_url, settings.fallback_model), ("https://b.example/v1", "deepseek-v4.1-flash"))
        self.assertNotIn("backup-secret", repr(settings))

    @patch.dict(os.environ, {"EXA_API_KEY": "  exa-secret \n"}, clear=True)
    def test_exa_key_read_and_hidden_from_repr(self):
        settings = Settings.from_env()
        self.assertEqual(settings.exa_api_key, "exa-secret")
        text = repr(Settings(api_key="model-secret", exa_api_key="exa-secret"))
        for secret in ("model-secret", "exa-secret"):
            self.assertNotIn(secret, text)


    def test_empty_data_dir_falls_back_to_var(self):
        from src.config import ROOT, data_dir
        with patch.dict(os.environ, {"JIANWEI_DATA": ""}):
            self.assertEqual(data_dir(), ROOT / "var")


class ModelClientTests(unittest.TestCase):
    settings = Settings(mode="api", base_url="https://api.example", model="test-model", api_key="test-secret")

    @patch("src.model_client.urllib.request.urlopen")
    def test_request_shape_and_key_only_in_header(self, urlopen):
        envelope = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
        urlopen.return_value = response(envelope)
        self.assertEqual(complete_json([], self.settings), {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example/chat/completions")
        self.assertEqual(json.loads(request.data)["model"], "test-model")
        self.assertNotIn("thinking", json.loads(request.data))
        self.assertNotIn("test-secret", request.data.decode())

    @patch("src.model_client.urllib.request.urlopen")
    def test_deepseek_disables_thinking(self, urlopen):
        urlopen.return_value = response({"choices": [{"message": {"content": "{}"}}]})
        settings = Settings(mode="api", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="k")
        complete_json([], settings)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["thinking"], {"type": "disabled"})

    @patch("src.model_client.urllib.request.urlopen")
    def test_http_error_hides_provider_body(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example", 429, "test-secret", {}, io.BytesIO(b"test-secret"))
        with self.assertRaises(ModelError) as error:
            complete_json([], self.settings)
        self.assertIn("429", str(error.exception))
        self.assertNotIn("test-secret", str(error.exception))

    @patch("src.model_client.urllib.request.urlopen")
    def test_truncated_completion_rejected(self, urlopen):
        urlopen.return_value = response(
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
        with self.assertRaises(ModelError):
            complete_json([], self.settings)


class NewsTests(unittest.TestCase):
    def test_parse_results_skips_malformed_items(self):
        rows = parse_results({"results": [
            {"url": "https://www.cls.cn/a", "title": "宇树涨停", "publishedDate": "2026-10-23T06:00:00.000Z", "text": "正文\n第二行"},
            {"url": "javascript:alert(1)", "title": "bad"}, {"title": "no url"}, "not a dict"]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "www.cls.cn")
        self.assertEqual(rows[0]["date"], "2026-10-23")
        self.assertEqual(rows[0]["text"], "正文 第二行")

    @patch("src.news.urllib.request.urlopen")
    def test_request_contract_and_cost(self, urlopen):
        urlopen.return_value = response({"results": [], "costDollars": {"total": 0.007},
                                         "resolvedSearchType": "neural", "answer": "合成回答"})
        result = search_news("白银有色 减持", "exa-secret", search_type="deep", num=3)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.headers["X-api-key"], "exa-secret")
        self.assertEqual((body["type"], body["numResults"]), ("deep", 3))
        self.assertIn("startPublishedDate", body)
        self.assertLess(body["startPublishedDate"], body["endPublishedDate"])
        self.assertEqual(result["cost_usd"], 0.007)
        self.assertEqual(result["resolved_type"], "neural")
        self.assertEqual(result["other"], {"answer": "合成回答"})

    @patch("src.news.urllib.request.urlopen")
    def test_errors_redact_key(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError("https://api.exa.ai/search", 401, "x", {},
                                                     io.BytesIO(b"invalid key exa-secret"))
        with self.assertRaises(NewsError) as error:
            search_news("q", "exa-secret")
        self.assertIn("401", str(error.exception))
        self.assertNotIn("exa-secret", str(error.exception))
        urlopen.side_effect = TimeoutError()
        with self.assertRaises(NewsError):
            search_news("q", "exa-secret")

    @patch("src.news.urllib.request.urlopen")
    def test_missing_key_never_requests(self, urlopen):
        with self.assertRaises(NewsError):
            search_news("q", "")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
