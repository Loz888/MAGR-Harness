"""Persistent per-task inbox for mid-run instructions and ask_user answers.

Messages are client-idempotent: re-submitting the same ``message_id`` with the
same content returns the original record and never applies it twice.  A
different payload under an existing id is rejected as a conflict.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mgar_v3.domain import DomainError

InboxKind = Literal["instruction", "answer", "note", "question"]
InboxStatus = Literal["received", "waiting", "applied", "ignored"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskInbox:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.is_file():
            self._save({"revision": 0, "messages": []})

    # -- persistence ----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"revision": 0, "messages": []}
        if not isinstance(payload, dict):
            return {"revision": 0, "messages": []}
        payload.setdefault("revision", 0)
        payload.setdefault("messages", [])
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    # -- public projection ---------------------------------------------

    @property
    def revision(self) -> int:
        with self._lock:
            payload = self._load()
            value = payload.get("revision", 0)
            return value if isinstance(value, int) else 0

    def messages(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load().get("messages", []))

    def pending_instructions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                message
                for message in self._load().get("messages", [])
                if message.get("kind") == "instruction"
                and message.get("status") == "received"
            ]

    def pending_answers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                message
                for message in self._load().get("messages", [])
                if message.get("kind") == "answer"
                and message.get("status") == "received"
            ]

    # -- mutation -------------------------------------------------------

    def post(
        self,
        *,
        message_id: str,
        kind: InboxKind,
        text: str,
        reply_to: str = "",
        client_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        value = text.strip()
        if not value:
            raise DomainError("HARNESS_MESSAGE_EMPTY", "The message is empty", 422)
        if kind not in {"instruction", "answer", "note"}:
            raise DomainError(
                "HARNESS_MESSAGE_KIND",
                "Only instruction, answer and note messages are accepted",
                422,
            )
        with self._lock:
            payload = self._load()
            messages: list[dict[str, Any]] = payload.get("messages", [])
            for message in messages:
                if message.get("message_id") != message_id:
                    continue
                same = (
                    message.get("kind") == kind
                    and message.get("text") == value
                    and message.get("reply_to", "") == reply_to
                )
                if same:
                    return dict(message), True
                raise DomainError(
                    "HARNESS_MESSAGE_CONFLICT",
                    "This message id already exists with different content",
                    409,
                )
            status: InboxStatus = "received"
            answered = False
            if kind == "answer" and reply_to:
                for message in messages:
                    if (
                        message.get("kind") == "answer"
                        and message.get("reply_to") == reply_to
                        and message.get("status") == "applied"
                    ):
                        answered = True
                        break
            if answered:
                status = "ignored"
            record = {
                "message_id": message_id,
                "kind": kind,
                "text": value,
                "reply_to": reply_to,
                "client_id": client_id,
                "status": status,
                "received_at": _now(),
                "applied_at": "",
                "applied_revision": 0,
            }
            messages.append(record)
            payload["messages"] = messages
            self._save(payload)
            return dict(record), False

    def add_question(self, *, question_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            payload = self._load()
            messages: list[dict[str, Any]] = payload.get("messages", [])
            for message in messages:
                if message.get("message_id") == question_id:
                    return dict(message)
            record = {
                "message_id": question_id,
                "kind": "question",
                "text": text.strip(),
                "reply_to": "",
                "client_id": "harness",
                "status": "waiting",
                "received_at": _now(),
                "applied_at": "",
                "applied_revision": 0,
            }
            messages.append(record)
            payload["messages"] = messages
            self._save(payload)
            return dict(record)

    def mark_applied(self, message_id: str, revision: int) -> dict[str, Any] | None:
        with self._lock:
            payload = self._load()
            for message in payload.get("messages", []):
                if message.get("message_id") != message_id:
                    continue
                if message.get("status") == "applied":
                    return dict(message)
                message["status"] = "applied"
                message["applied_at"] = _now()
                message["applied_revision"] = int(revision)
                payload["revision"] = max(
                    int(payload.get("revision", 0)),
                    int(revision),
                )
                self._save(payload)
                return dict(message)
            return None

    def mark_ignored(self, message_id: str) -> None:
        with self._lock:
            payload = self._load()
            for message in payload.get("messages", []):
                if message.get("message_id") == message_id:
                    message["status"] = "ignored"
            self._save(payload)

    def answer_for(self, question_id: str) -> dict[str, Any] | None:
        with self._lock:
            for message in self._load().get("messages", []):
                if (
                    message.get("kind") == "answer"
                    and message.get("reply_to") == question_id
                    and message.get("status") in {"received", "applied"}
                ):
                    return dict(message)
            return None

    def wait_for_answer(
        self,
        question_id: str,
        control: Any,
        timeout: float = 600.0,
    ) -> dict[str, Any] | None:
        """Block until an answer arrives, the task is cancelled, or timeout."""

        deadline = time.monotonic() + timeout
        while True:
            answer = self.answer_for(question_id)
            if answer is not None:
                return answer
            if control.cancel_requested:
                raise DomainError(
                    "HARNESS_CANCELLED",
                    "The task was cancelled while waiting for an answer",
                    409,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            control.wait(min(0.2, remaining))
