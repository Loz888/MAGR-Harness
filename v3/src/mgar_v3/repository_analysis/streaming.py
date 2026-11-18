"""Incremental provider SSE decoding and public report previews (never model reasoning)."""

from __future__ import annotations

import codecs
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 512 * 1024
MAX_FRAME_BYTES = 512 * 1024
MAX_WIRE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class WireReceipt:
    """Transport accounting without retaining provider metadata or reasoning."""

    wire_bytes: int
    wire_sha256: str
    frames: int
    content_bytes: int
    completion_boundary: str


class StreamReadError(ValueError):
    def __init__(self, reason: str, diagnostics: dict[str, Any]):
        # No source/provider text can enter an error event or exception message.
        super().__init__("Stream ended without a complete usage receipt: " + reason)
        self.reason = reason
        self.diagnostics = diagnostics


class ReportPreview:
    """Track JSON string paths while a structured completion is still arriving."""

    def __init__(self) -> None:
        self.stack: list[dict[str, Any]] = []
        self.in_string = False
        self.escaped = False
        self.raw = ""
        self.path: tuple[str, ...] = ()
        self.is_key = False
        self.values: dict[str, str] = {}

    def _value_path(self) -> tuple[str, ...]:
        if not self.stack:
            return ()
        top = self.stack[-1]
        return tuple(top["path"]) + (str(top.get("key") or "[]"),)

    def _decode(self) -> None:
        if self.is_key or self.path not in {("report", "conclusion"), ("report", "structure")}:
            return
        for trim in range(min(7, len(self.raw) + 1)):
            text = self.raw[:-trim] if trim else self.raw
            try:
                value = json.loads('"' + text + '"')
            except ValueError:
                continue
            # A high surrogate may arrive before its low surrogate in the next chunk.
            if value and 0xD800 <= ord(value[-1]) <= 0xDFFF:
                value = value[:-1]
            self.values[self.path[-1]] = value
            break

    def feed(self, fragment: str) -> dict[str, str]:
        for char in fragment:
            if self.in_string:
                if char == '"' and not self.escaped:
                    self.in_string = False
                    if self.is_key and self.stack:
                        try:
                            self.stack[-1]["key"] = json.loads('"' + self.raw + '"')
                            self.stack[-1]["key_expected"] = False
                        except ValueError:
                            pass
                    else:
                        self._decode()
                    continue
                self.raw += char
                self.escaped = not self.escaped if char == "\\" else False
                continue
            if char == '"':
                self.in_string = True
                self.raw = ""
                self.escaped = False
                self.is_key = bool(self.stack and self.stack[-1].get("key_expected"))
                self.path = self._value_path()
            elif char in "{[":
                self.stack.append(
                    {
                        "path": self._value_path(),
                        "key_expected": char == "{",
                        "object": char == "{",
                    }
                )
            elif char in "}]":
                if self.stack:
                    self.stack.pop()
            elif char == "," and self.stack and self.stack[-1]["object"]:
                self.stack[-1]["key_expected"] = True
                self.stack[-1]["key"] = None
        if self.in_string:
            self._decode()
        return dict(self.values)


class PreviewRelay:
    def __init__(self, send: Callable[[str, str, int], None]):
        self.preview = ReportPreview()
        self.send = send
        self.sent: dict[str, str] = {}
        self.last_flush = time.monotonic()

    def flush(self) -> None:
        for field, text in self.preview.values.items():
            old = self.sent.get(field, "")
            if text == old:
                continue
            offset = len(old) if text.startswith(old) else 0
            self.send(field, text[offset:], offset)
            self.sent[field] = text
        self.last_flush = time.monotonic()

    def feed(self, fragment: str) -> None:
        self.preview.feed(fragment)
        growth = sum(
            len(text) - len(self.sent.get(field, "")) for field, text in self.preview.values.items()
        )
        if not self.sent or growth >= 256 or time.monotonic() - self.last_flush >= 0.15:
            self.flush()


def read_completion(
    response: httpx.Response,
    on_content: Callable[[str], None],
    check_deadline: Callable[[], None],
    *,
    max_output_tokens: int = 8192,
) -> tuple[dict[str, Any], WireReceipt]:
    """Decode bounded semantic content; hash protocol overhead incrementally.

    Metadata repeated on every token is not output text. Its total byte limit
    follows the requested output allowance, with separate frame/content limits.
    A finish and valid usage at/after that finish form the terminal receipt;
    the optional DONE sentinel is not itself evidence of billable token usage.
    """
    wire_limit = min(MAX_WIRE_BYTES, max(2 * 1024 * 1024, max_output_tokens * 4096))
    frame_limit = max(4096, max_output_tokens * 16)
    wire_bytes = 0
    wire_hash = hashlib.sha256()
    frames = 0
    content_bytes = 0
    model: str | None = None
    usage: dict[str, Any] | None = None
    finish: str | None = None
    done = False
    terminal_usage = False

    def failure(reason: str) -> StreamReadError:
        return StreamReadError(reason, {
            "reason": reason, "wire_bytes": wire_bytes,
            "wire_sha256": wire_hash.hexdigest(), "frames": frames,
            "content_bytes": content_bytes, "done_received": done,
            "finish_received": finish is not None, "usage_received": usage is not None,
            "terminal_usage_received": terminal_usage, "wire_limit_bytes": wire_limit,
            "frame_limit_bytes": MAX_FRAME_BYTES, "content_limit_bytes": MAX_RESPONSE_BYTES,
        })

    def consume(chunk: bytes) -> None:
        nonlocal wire_bytes
        wire_bytes += len(chunk)
        wire_hash.update(chunk)
        if wire_bytes > wire_limit:
            raise failure("wire_limit")

    def receipt(boundary: str) -> WireReceipt:
        return WireReceipt(wire_bytes, wire_hash.hexdigest(), frames, content_bytes, boundary)

    if "text/event-stream" not in response.headers.get("content-type", ""):
        plain = bytearray()
        try:
            for chunk in response.iter_bytes():
                check_deadline()
                consume(chunk)
                if len(plain) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise failure("json_response_limit")
                plain.extend(chunk)
            value = json.loads(plain)
        except httpx.HTTPError:
            raise failure("transport_error") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise failure("invalid_json") from None
        if not isinstance(value, dict):
            raise failure("invalid_json_shape")
        return value, receipt("json_eof")
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data: list[str] = []
    content: list[str] = []
    frame_bytes = 0

    def valid_usage(value: dict[str, Any] | None) -> bool:
        return isinstance(value, dict) and all(
            type(value.get(name)) is int and value[name] >= 0
            for name in ("prompt_tokens", "completion_tokens")
        )

    def frame() -> None:
        nonlocal model, usage, finish, done, frames, content_bytes, terminal_usage
        frames += 1
        if frames > frame_limit:
            raise failure("frame_count_limit")
        if not data:
            return
        payload = "\n".join(data)
        data.clear()
        if payload == "[DONE]":
            done = True
            return
        if done:
            raise failure("data_after_done")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            raise failure("invalid_json_frame") from None
        if not isinstance(value, dict):
            raise failure("invalid_frame")
        if value.get("model"):
            if not isinstance(value["model"], str) or len(value["model"]) > 200:
                raise failure("invalid_model")
            if model is not None and model != value["model"]:
                raise failure("model_changed")
            model = value["model"]
        choices = value.get("choices") or []
        if not isinstance(choices, list):
            raise failure("invalid_choices")
        for choice in choices:
            if not isinstance(choice, dict):
                raise failure("invalid_choice")
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise failure("invalid_delta")
            text = delta.get("content")
            if text:
                if not isinstance(text, str):
                    raise failure("invalid_content")
                if finish is not None:
                    raise failure("content_after_finish")
                content_bytes += len(text.encode("utf-8"))
                if content_bytes > MAX_RESPONSE_BYTES:
                    raise failure("content_limit")
                content.append(text)
                on_content(text)
            if choice.get("finish_reason") is not None:
                if not isinstance(choice["finish_reason"], str):
                    raise failure("invalid_finish")
                finish = choice["finish_reason"]
        if isinstance(value.get("usage"), dict):
            usage = value["usage"]
            terminal_usage = finish is not None and valid_usage(usage)

    def line(value: str) -> None:
        nonlocal frame_bytes
        frame_bytes += len(value.encode("utf-8")) + 1
        if frame_bytes > MAX_FRAME_BYTES:
            raise failure("frame_limit")
        value = value.rstrip("\r")
        if not value:
            frame()
            frame_bytes = 0
        elif value.startswith("data:"):
            data.append(value[5:].lstrip(" "))

    def complete(boundary: str) -> tuple[dict[str, Any], WireReceipt]:
        if finish is None:
            raise failure("missing_finish")
        if not valid_usage(usage):
            raise failure("missing_usage")
        if not terminal_usage:
            raise failure("usage_before_finish")
        if not model:
            raise failure("missing_model")
        return {
            "model": model, "usage": usage,
            "choices": [{"finish_reason": finish, "message": {"content": "".join(content)}}],
        }, receipt(boundary)

    try:
        for chunk in response.iter_bytes():
            check_deadline()
            consume(chunk)
            # An HTTP transport may hand over a very large chunk. Decode in
            # slices so a huge SSE line cannot create an unbounded text buffer.
            for offset in range(0, len(chunk), 16384):
                pending += decoder.decode(chunk[offset:offset + 16384])
                while "\n" in pending:
                    value, pending = pending.split("\n", 1)
                    line(value)
                if len(pending.encode("utf-8")) + frame_bytes > MAX_FRAME_BYTES:
                    raise failure("frame_limit")
        pending += decoder.decode(b"", final=True)
        if pending:
            line(pending)
        if data:
            frame()
    except httpx.HTTPError:
        if terminal_usage and finish is not None and model:
            return complete("terminal_usage_transport_end")
        raise failure("transport_error") from None
    except UnicodeDecodeError:
        raise failure("invalid_encoding") from None
    return complete("done" if done else "terminal_usage_eof")
