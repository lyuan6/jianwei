"""OpenAI-compatible chat completion client."""
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


# 备用只在主服务不可用时启用；切换后暂停主服务一段时间，免得一次分析的每一步都先等它超时。
PRIMARY_TIMEOUT = 75
PRIMARY_COOLDOWN = 120
_primary_down_until = 0.0

logger = logging.getLogger(__name__)


class ModelError(ValueError):
    pass


class ModelUnavailable(ModelError):
    pass


def _send(body, base_url, api_key, timeout):
    if urlparse(base_url).hostname == "api.deepseek.com" or "deepseek" in body["model"]:
        body = {**body, "thinking": {"type": "disabled"}}
    request = urllib.request.Request(base_url + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (402, 429) or exc.code >= 500:
            raise ModelUnavailable(f"模型服务繁忙（HTTP {exc.code}），请稍后重试。") from None
        raise ModelError(f"模型服务返回 HTTP {exc.code}；请检查密钥、模型权限。") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ModelUnavailable("模型服务连接失败或超时；请求可能已产生用量，请检查供应商记录后重试。") from None
    except ValueError:
        raise ModelError("模型响应不是预期的 JSON 结构。") from None


def _request(body, settings, timeout):
    global _primary_down_until
    if not settings.fallback_api_key:
        return _send(body, settings.base_url, settings.api_key, timeout)
    if time.monotonic() >= _primary_down_until:
        try:
            return _send(body, settings.base_url, settings.api_key, min(timeout, PRIMARY_TIMEOUT))
        except ModelUnavailable as exc:
            _primary_down_until = time.monotonic() + PRIMARY_COOLDOWN
            logger.warning("主模型不可用，切换备用 %s 秒：%s", PRIMARY_COOLDOWN, exc)
    return _send({**body, "model": settings.fallback_model}, settings.fallback_base_url, settings.fallback_api_key, timeout)


def chat(messages, settings, tools=None, *, max_tokens=2200, timeout=60, json_mode=False):
    if settings.mode != "api":
        raise ModelError("当前为离线模式；配置 APP_MODE=api 和模型参数后可使用 AI 分析。")
    body = {"model": settings.model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    envelope = _request(body, settings, timeout)
    try:
        choice = envelope["choices"][0]
        message = choice["message"]
        content, calls = message.get("content"), message.get("tool_calls") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ModelError("模型响应不是预期的 JSON 结构。") from None
    if choice.get("finish_reason") not in (None, "stop", "tool_calls"):
        raise ModelError("模型响应未完整结束，未采用该回答。")
    try:
        normalized = [{"id": call["id"], "type": "function", "function": {
            "name": call["function"]["name"], "arguments": call["function"]["arguments"]}} for call in calls]
    except (KeyError, TypeError):
        raise ModelError("模型工具调用格式无效。") from None
    if not normalized and not isinstance(content, str):
        raise ModelError("模型没有返回正文或工具调用。")
    return {"content": content, "tool_calls": normalized, "usage": envelope.get("usage") or {}}


def complete_json(messages, settings, *, max_tokens=2200, timeout=35):
    response = chat(messages, settings, max_tokens=max_tokens, timeout=timeout, json_mode=True)
    try:
        result = json.loads(response["content"])
    except (ValueError, TypeError):
        raise ModelError("模型响应不是预期的 JSON 结构。") from None
    if not isinstance(result, dict):
        raise ModelError("模型输出不是 JSON 对象。")
    return result
