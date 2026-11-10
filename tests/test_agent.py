import copy
import io
import json
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import MagicMock, patch

from src import agent, model_client
from src.config import Settings
from src.market import TZ
from src.tools import Tool, build_tools, compare, get_price_history


SETTINGS = Settings(mode="api", base_url="https://api.deepseek.com", model="test", api_key="private-test-key")


def response(payload):
    handle = MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return handle


def call(name="echo", arguments=None, number=1):
    return {"id": f"call_{number}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments if arguments is not None else {"text": "hello"})}}


def reply(content=None, calls=None):
    return {"content": content, "tool_calls": calls or [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def echo_tools(function=None):
    return {"echo": Tool("echo", "回显", {"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"], "additionalProperties": False}, function or (lambda text: {"text": text}))}


class ChatTests(unittest.TestCase):
    @patch("src.model_client.urllib.request.urlopen")
    def test_tools_round_trip_and_non_thinking_request(self, urlopen):
        urlopen.side_effect = [
            response({"choices": [{"finish_reason": "tool_calls", "message": reply(calls=[call()])}], "usage": {"prompt_tokens": 10}}),
            response({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}),
        ]
        result = agent.run("返回 JSON", [], echo_tools(), 1, settings=SETTINGS)
        self.assertEqual(result["result"], {"ok": True})
        self.assertEqual(result["references"], {"R1": {"text": "hello"}})
        bodies = [json.loads(item.args[0].data) for item in urlopen.call_args_list]
        self.assertEqual(bodies[0]["thinking"], {"type": "disabled"})
        self.assertEqual(bodies[0]["tools"][0]["function"]["name"], "echo")
        self.assertNotIn("response_format", bodies[0])
        self.assertNotIn("tools", bodies[1])
        self.assertEqual(bodies[1]["response_format"], {"type": "json_object"})
        tool_message = next(item for item in bodies[1]["messages"] if item["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tool_message["content"])["ref"], "R1")
        self.assertNotIn(SETTINGS.api_key, json.dumps(bodies))

    @patch("src.model_client.urllib.request.urlopen")
    def test_no_retry_or_key_disclosure_on_request_failure(self, urlopen):
        for error in (TimeoutError(), urllib.error.HTTPError("https://api.deepseek.com", 401, SETTINGS.api_key, {}, io.BytesIO(SETTINGS.api_key.encode()))):
            with self.subTest(error=type(error).__name__):
                urlopen.reset_mock()
                urlopen.side_effect = error
                with self.assertRaises(model_client.ModelError) as raised:
                    model_client.chat([], SETTINGS)
                self.assertNotIn(SETTINGS.api_key, str(raised.exception))
                self.assertEqual(urlopen.call_count, 1)

    @patch("src.model_client.urllib.request.urlopen")
    def test_incomplete_or_malformed_response_fails(self, urlopen):
        for choice in (
            {"finish_reason": "length", "message": {"content": "{}"}},
            {"finish_reason": "tool_calls", "message": {"tool_calls": []}},
        ):
            with self.subTest(choice=choice):
                urlopen.return_value = response({"choices": [choice]})
                with self.assertRaises(model_client.ModelError):
                    model_client.chat([], SETTINGS)


FALLBACK = Settings(mode="api", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="primary-key",
                    fallback_base_url="https://backup.example/v1", fallback_model="deepseek-v4.1-flash", fallback_api_key="backup-key")


def http_error(code):
    return urllib.error.HTTPError("https://api.deepseek.com", code, "error", {}, io.BytesIO(b""))


@patch("src.model_client.urllib.request.urlopen")
class FallbackTests(unittest.TestCase):
    OK = {"choices": [{"finish_reason": "stop", "message": {"content": "好"}}]}

    def setUp(self):
        model_client._primary_down_until = 0.0
        self.addCleanup(setattr, model_client, "_primary_down_until", 0.0)

    def test_busy_primary_switches_and_stays_on_fallback_until_cooldown(self, urlopen):
        for error in (http_error(503), http_error(429), TimeoutError()):
            with self.subTest(error=error):
                model_client._primary_down_until = 0.0
                urlopen.reset_mock()
                urlopen.side_effect = [error, response(self.OK)]
                self.assertEqual(model_client.chat([], FALLBACK)["content"], "好")
                primary, backup = [item.args[0] for item in urlopen.call_args_list]
                self.assertEqual(primary.full_url, "https://api.deepseek.com/chat/completions")
                self.assertEqual(backup.full_url, "https://backup.example/v1/chat/completions")
                self.assertEqual(backup.get_header("Authorization"), "Bearer backup-key")
                body = json.loads(backup.data)
                self.assertEqual(body["model"], "deepseek-v4.1-flash")
                self.assertEqual(body["thinking"], {"type": "disabled"})
        urlopen.reset_mock()
        urlopen.side_effect = [response(self.OK)]
        model_client.chat([], FALLBACK)
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://backup.example/v1/chat/completions")
        model_client._primary_down_until = 0.0
        urlopen.side_effect = [response(self.OK)]
        model_client.chat([], FALLBACK)
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://api.deepseek.com/chat/completions")

    def test_auth_error_does_not_switch(self, urlopen):
        urlopen.side_effect = http_error(401)
        with self.assertRaises(model_client.ModelError):
            model_client.chat([], FALLBACK)
        self.assertEqual(urlopen.call_count, 1)

    def test_both_down_fails_without_disclosing_keys(self, urlopen):
        urlopen.side_effect = [http_error(503), http_error(503)]
        with self.assertRaises(model_client.ModelError) as raised:
            model_client.chat([], FALLBACK)
        self.assertIn("繁忙", str(raised.exception))
        self.assertEqual(urlopen.call_count, 2)
        self.assertNotIn("backup-key", str(raised.exception))


class AgentTests(unittest.TestCase):
    @patch("src.agent.model_client.chat")
    def test_tool_failure_is_returned_to_model_without_retry(self, chat):
        function = MagicMock(side_effect=ValueError("事件数据源暂时不可用。"))
        chat.side_effect = [reply(calls=[call()]), reply('{"gaps":["数据不可用"]}')]
        result = agent.run("任务", [], echo_tools(function), 6, settings=SETTINGS)
        function.assert_called_once_with(text="hello")
        self.assertTrue(result["steps"][0]["error"])
        self.assertIn("数据源", result["steps"][0]["summary"])
        self.assertIn("error", result["references"]["R1"])
        self.assertGreaterEqual(result["steps"][0]["elapsed_seconds"], 0)
        history = chat.call_args.args[0]
        self.assertIn("数据源", next(item["content"] for item in history if item["role"] == "tool"))

    def test_invalid_tool_or_arguments_recorded_and_not_executed(self):
        function = MagicMock()
        paper = agent.Workpaper()
        for name, arguments in (("missing", {}), ("echo", "{"), ("echo", {"wrong": 1}), ("echo", {"text": 5})):
            result = paper.execute(echo_tools(function), name, arguments)
            self.assertIn("error", result["result"])
        function.assert_not_called()
        self.assertEqual(list(paper.references), ["R1", "R2", "R3", "R4"])

    @patch("src.agent.model_client.chat")
    def test_batch_counts_individual_calls_and_responds_to_overflow(self, chat):
        function = MagicMock(return_value={"text": "ok"})
        chat.side_effect = [reply(calls=[call(number=i) for i in range(8)]), reply('{"events":[]}')]
        paper = agent.Workpaper()
        paper.execute(echo_tools(), "echo", {"text": "预取"})
        result = agent.run("任务", [], echo_tools(function), 6, settings=SETTINGS, workpaper=paper)
        self.assertEqual(function.call_count, 6)
        self.assertEqual(list(result["references"]), [f"R{i}" for i in range(1, 8)])
        history = chat.call_args.args[0]
        messages = [item for item in history if item["role"] == "tool"]
        self.assertEqual(len(messages), 8)
        self.assertIn("error", json.loads(messages[-1]["content"]))
        self.assertIsNone(chat.call_args.kwargs["tools"])
        self.assertIn("次数已用完", history[-1]["content"])

    @patch("src.agent.model_client.chat")
    def test_multiple_rounds_use_returned_data_and_do_not_modify_input(self, chat):
        histories = []
        answers = iter([reply(calls=[call()]), reply(calls=[call(arguments={"text": "followup"}, number=2)]), reply('{"ok":true}')])
        def respond(messages, *args, **kwargs):
            histories.append(copy.deepcopy(messages))
            return next(answers)
        chat.side_effect = respond
        original = [{"role": "user", "content": "资料"}]
        result = agent.run("任务", original, echo_tools(), 6, settings=SETTINGS)
        self.assertEqual(original, [{"role": "user", "content": "资料"}])
        self.assertEqual(result["references"]["R2"], {"text": "followup"})
        self.assertEqual(sum(item["role"] == "tool" for item in histories[2]), 2)
        self.assertEqual(len(result["usage"]), 3)

    @patch("src.agent.model_client.chat")
    def test_invalid_json_corrected_once_without_tools(self, chat):
        chat.side_effect = [reply("not JSON"), reply('{"ok":true}')]
        self.assertEqual(agent.run("任务", [], echo_tools(), 6, settings=SETTINGS)["result"], {"ok": True})
        self.assertNotIn("tools", chat.call_args.kwargs)
        chat.reset_mock()
        chat.side_effect = [reply("not JSON"), reply("[]")]
        with self.assertRaises(agent.OutputError):
            agent.run("任务", [], echo_tools(), 6, settings=SETTINGS)
        self.assertEqual(chat.call_count, 2)

    @patch("src.agent.model_client.chat")
    def test_model_network_failure_is_not_a_format_retry(self, chat):
        chat.side_effect = model_client.ModelError("模型连接失败。")
        with self.assertRaises(model_client.ModelError):
            agent.run("任务", [], echo_tools(), 6, settings=SETTINGS)
        chat.assert_called_once()


class ToolsTests(unittest.TestCase):
    def test_registry_and_holdings_are_scoped_to_research(self):
        self.assertEqual(set(build_tools(SETTINGS)), {"list_announcements", "read_announcement", "get_unlocks",
                         "get_holder_changes", "get_forecasts", "get_financials", "get_price_history", "compare", "search_news"})
        self.assertEqual(build_tools(SETTINGS, holdings=[])["get_holdings"].invoke({}), [])

    @patch("src.tools.news.search_news")
    @patch("src.tools.events.fetch_announcements")
    def test_adapters_validate_arguments_and_pass_news_key(self, announcements, search):
        settings = Settings(exa_api_key="exa-test-key")
        tools = build_tools(settings)
        announcements.return_value = [{"title": "公告"}]
        self.assertEqual(tools["list_announcements"].invoke({"code": "sz002079", "days": 3}), [{"title": "公告"}])
        announcements.assert_called_once_with("sz002079", days=3)
        tools["search_news"].invoke({"query": "解禁", "days": 7})
        search.assert_called_once_with("解禁", "exa-test-key", days=7)
        with self.assertRaises(ValueError):
            tools["list_announcements"].invoke({"code": "sz002079", "days": -3})
        with self.assertRaises(ValueError):
            tools["compare"].invoke({"codes": [], "days": 7})
        self.assertNotIn("exa-test-key", json.dumps([tool.schema() for tool in tools.values()]))

    @patch("src.market.now", return_value=datetime(2026, 10, 26, tzinfo=TZ))
    @patch("src.tools.market.fetch_history")
    def test_price_and_comparison_use_decimal_and_common_dates(self, fetch, now):
        def history(code):
            bars = [{"date": f"2026-10-{day}", "close": close} for day, close in ((21, "100"), (22, "90"), (23, "110"))]
            if code == "sh000300":
                bars = bars[1:]
            return {"symbol": code, "source": "腾讯", "url": "https://gu.qq.com/" + code,
                    "adjustment": "指数点位" if code == "sh000300" else "前复权", "bars": bars}
        fetch.side_effect = history
        result = get_price_history("sz002079", 7)
        self.assertEqual((result["return_pct"], result["max_drawdown_pct"]), ("10.0000", "-10.0000"))
        self.assertEqual((result["first_close"], result["last_close"]), ("100", "110"))
        rows = compare(["sz002079", "sh000300"], 7)
        self.assertEqual([row["start"] for row in rows], ["2026-10-22"] * 2)
        self.assertEqual([row["return_pct"] for row in rows], ["22.2222"] * 2)


if __name__ == "__main__":
    unittest.main()
