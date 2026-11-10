"""有调用上限和工作底稿的工具循环。"""

import json
import time

from src import model_client, prompts


class OutputError(ValueError):
    pass


class Workpaper:
    def __init__(self, on_step=None):
        self.steps = []
        self.references = {}
        self.on_step = on_step

    def execute(self, tools, name, arguments, *, result_filter=None):
        started = time.monotonic()
        ref = f"R{len(self.steps) + 1}"
        error = False
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if name not in tools:
                raise ValueError("工具不存在。")
            tool = tools[name]
            result = tool.invoke(arguments)
            if result_filter is not None:
                result = result_filter(result)
            summary = tool.summary(result)
        except Exception as exc:
            # 工具是外部调用边界；失败也交给模型，不能伪装成空数据。
            error = True
            summary = str(exc) if isinstance(exc, ValueError) else "工具执行失败。"
            result = {"error": summary}
        self.references[ref] = result
        self.steps.append({"tool": name, "arguments": arguments, "summary": summary, "ref": ref,
                           "elapsed_seconds": round(time.monotonic() - started, 3), "error": error})
        if self.on_step is not None:
            self.on_step(self.steps[-1])
        return {"ref": ref, "result": result}


def _parse(response, references, validate):
    if response["tool_calls"]:
        raise OutputError("需要最终 JSON 结论，不能继续调用工具。")
    try:
        result = json.loads(response["content"])
    except (ValueError, TypeError):
        raise OutputError("输出不是有效 JSON。") from None
    if not isinstance(result, dict):
        raise OutputError("输出必须是 JSON 对象。")
    return validate(result, references) if validate else result


def run(task, messages, tools, max_calls, *, settings, workpaper=None, validate=None, max_tokens=2200, timeout=60):
    workpaper = workpaper if workpaper is not None else Workpaper()
    history = [{"role": "system", "content": prompts.AGENT}, *messages, {"role": "user", "content": task}]
    usage, count = [], 0
    schemas = [tool.schema() for tool in tools.values()]
    while True:
        available = count < max_calls and bool(schemas)
        if not available:
            history.append({"role": "user", "content": prompts.CALL_LIMIT})
        response = model_client.chat(history, settings, tools=schemas if available else None, json_mode=not available, max_tokens=max_tokens, timeout=timeout)
        usage.append(response["usage"])
        calls = response["tool_calls"]
        if calls and available:
            history.append({"role": "assistant", "content": response["content"], "tool_calls": calls})
            for call in calls:
                if count < max_calls:
                    count += 1
                    result = workpaper.execute(tools, call["function"]["name"], call["function"]["arguments"])
                else:
                    result = {"error": prompts.CALL_LIMIT}
                history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            continue
        try:
            result = _parse(response, workpaper.references, validate)
        except OutputError as exc:
            history.append({"role": "assistant", "content": response["content"] or ""})
            history.append({"role": "user", "content": prompts.REPAIR.format(error=exc)})
            response = model_client.chat(history, settings, json_mode=True, max_tokens=max_tokens, timeout=timeout)
            usage.append(response["usage"])
            result = _parse(response, workpaper.references, validate)
        return {"result": result, "steps": workpaper.steps, "references": workpaper.references, "usage": usage}


def complete(messages, settings, validate, *, max_tokens=2200):
    """不带工具的一次 JSON 请求；结构错误修正一次。"""
    history = [{"role": "system", "content": prompts.AGENT}, *messages]
    response = model_client.chat(history, settings, max_tokens=max_tokens, json_mode=True)
    usage = [response["usage"]]
    try:
        result = _parse(response, {}, validate)
    except OutputError as exc:
        history += [{"role": "assistant", "content": response["content"] or ""},
                    {"role": "user", "content": prompts.REPAIR.format(error=exc)}]
        response = model_client.chat(history, settings, max_tokens=max_tokens, json_mode=True)
        usage.append(response["usage"])
        result = _parse(response, {}, validate)
    return {"result": result, "usage": usage}
