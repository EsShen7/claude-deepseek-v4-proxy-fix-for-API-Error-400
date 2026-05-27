#!/usr/bin/env python3
"""Local Anthropic Messages -> DeepSeek chat/completions proxy for Claude Code.

Claude Code talks Anthropic Messages to this local server. The server converts
requests to DeepSeek's OpenAI-compatible chat/completions API and disables
DeepSeek V4 thinking mode to avoid the 400 thinking pass-back error in tool
loops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def extract_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return as_text(blocks)

    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            typ = block.get("type")
            if typ == "text":
                parts.append(as_text(block.get("text")))
            elif typ == "tool_result":
                parts.append(extract_text(block.get("content")))
    return "\n".join(part for part in parts if part)


def normalize_model(model: str) -> str:
    lower = (model or "").lower()
    if "pro" in lower:
        return "deepseek-v4-pro"
    if "flash" in lower or "sonnet" in lower or "haiku" in lower:
        return "deepseek-v4-flash"
    if lower.startswith("deepseek-"):
        return lower
    return "deepseek-v4-flash"


def convert_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    result: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return result


def convert_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    typ = choice.get("type")
    if typ in (None, "auto"):
        return "auto"
    if typ == "none":
        return "none"
    if typ == "any":
        return "required"
    if typ == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def anthropic_messages_to_openai(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    if body.get("system"):
        messages.append({"role": "system", "content": extract_text(body["system"])})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant":
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]

            for block in blocks:
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ == "text":
                    text_parts.append(as_text(block.get("text")))
                elif typ == "thinking":
                    reasoning_parts.append(as_text(block.get("thinking") or block.get("text")))
                elif typ == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id") or f"call_{len(tool_calls) + 1}",
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                            },
                        }
                    )

            out: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if reasoning_parts:
                out["reasoning_content"] = "\n".join(reasoning_parts)
            elif tool_calls:
                out["reasoning_content"] = ""
            if tool_calls:
                out["tool_calls"] = tool_calls
            messages.append(out)

        elif role == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            user_parts: list[str] = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or block.get("id") or "",
                            "content": extract_text(block.get("content")),
                        }
                    )
                else:
                    user_parts.append(extract_text([block]))
            user_text = "\n".join(part for part in user_parts if part)
            if user_text:
                messages.append({"role": "user", "content": user_text})

    return messages


def build_deepseek_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": normalize_model(body.get("model", "")),
        "messages": anthropic_messages_to_openai(body),
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    if body.get("max_tokens"):
        payload["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]

    tools = convert_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        tool_choice = convert_tool_choice(body.get("tool_choice"))
        if tool_choice:
            payload["tool_choice"] = tool_choice
    return payload


def deepseek_to_anthropic(resp: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[dict[str, Any]] = []

    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})

    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except Exception:
            args = {"arguments": raw_args}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"call_{len(blocks) + 1}",
                "name": fn.get("name") or "",
                "input": args,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    usage = resp.get("usage") or {}
    stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else "end_turn"
    if choice.get("finish_reason") in {"length", "max_tokens"}:
        stop_reason = "max_tokens"

    return {
        "id": resp.get("id") or f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def empty_stream_block(block: dict[str, Any]) -> dict[str, Any]:
    if block["type"] == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}}
    return {"type": "text", "text": ""}


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "DeepSeekAnthropicProxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        if urlparse(self.path).path in {"/", "/health"}:
            self.send_json({"ok": True})
            return
        self.send_error(404)

    def do_HEAD(self) -> None:
        if urlparse(self.path).path in {"/", "/health", "/anthropic"}:
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not urlparse(self.path).path.endswith("/v1/messages"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length") or "0")
            body = json.loads(self.rfile.read(length) or b"{}")
            api_key = (
                self.headers.get("x-api-key")
                or self.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or os.environ.get("DEEPSEEK_API_KEY")
            )
            if not api_key:
                self.send_json({"error": {"message": "missing API key", "type": "invalid_request_error"}}, 401)
                return

            payload = build_deepseek_payload(body)
            response = self.call_deepseek(payload, api_key)
            message = deepseek_to_anthropic(response, body.get("model") or payload["model"])
            if body.get("stream"):
                self.send_sse(message)
            else:
                self.send_json(message)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.send_json({"error": {"message": detail, "type": "api_error"}}, exc.code)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": {"message": str(exc), "type": "proxy_error"}}, 500)

    def call_deepseek(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_json(self, obj: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_sse_event(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def send_sse(self, message: dict[str, Any]) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        start = dict(message)
        start["content"] = []
        start["stop_reason"] = None
        self.send_sse_event("message_start", {"type": "message_start", "message": start})

        for idx, block in enumerate(message["content"]):
            self.send_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": idx, "content_block": empty_stream_block(block)},
            )
            if block["type"] == "text" and block.get("text"):
                self.send_sse_event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": block["text"]}},
                )
            elif block["type"] == "tool_use":
                self.send_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    },
                )
            self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": idx})

        self.send_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": message.get("usage", {}).get("output_tokens", 0)},
            },
        )
        self.send_sse_event("message_stop", {"type": "message_stop"})
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    print(f"DeepSeek Anthropic proxy listening on http://{args.host}:{args.port}/anthropic", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
