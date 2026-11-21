"""Durable reserve-before-send journal for the single Phase 2 E3 grant."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from mgar_v3.models.deepseek_policy import deepseek_request_options
from mgar_v3.quality_eval.e1_protocol import encode, sha256
from mgar_v3.quality_eval.e2_live import _cost, _token_usage
from mgar_v3.quality_eval.e3_live import (
    ENDPOINT,
    MAX_OUTPUT,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MODEL,
    PROMPT,
)

MAX_CALLS = 2040
MAX_MICROS = 18_000_000
SYSTEM = (
    PROMPT + "每次根据 allowed_actions 决策。收到本次读取的证据后，应直接计算并提交。"
    "同一证据再次读取只返回已有收据。"
)


class Stopped(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path, freeze: str, key: str, client: httpx.Client):
        self.guard = (path.parent / "grant.lock").open("a")
        fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.key = key
        self.client = client
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS grant_meta(freeze TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests(
              trial TEXT PRIMARY KEY, request_id TEXT UNIQUE, body_sha TEXT, state TEXT,
              reserve INTEGER, cost INTEGER DEFAULT 0, started TEXT, finished TEXT,
              input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, content TEXT, usage TEXT,
              response_sha TEXT, provider_id TEXT, completion_id TEXT, model TEXT, finish TEXT,
              reason TEXT);
        """)
        freezes = [r[0] for r in self.db.execute("SELECT freeze FROM grant_meta")]
        if freezes and freezes != [freeze]:
            raise Stopped("FREEZE_MISMATCH")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO grant_meta VALUES (?)", (freeze,))
        self.halted = None
        if self.db.execute(
            "SELECT 1 FROM requests WHERE state != 'settled' OR reason IS NOT NULL LIMIT 1"
        ).fetchone():
            self.halted = "PREVIOUS_RECEIPT_REQUIRES_RECONCILIATION"

    def close(self):
        self.db.close()
        self.guard.close()

    def complete(self, trial, payload):
        body = encode(
            dict(
                model=MODEL,
                messages=[
                    dict(role="system", content=SYSTEM),
                    dict(role="user", content=encode(payload).decode()),
                ],
                stream=False,
                response_format={"type": "json_object"},
                max_tokens=MAX_OUTPUT,
                **deepseek_request_options(),
            )
        )
        if len(body) > MAX_REQUEST_BYTES:
            raise Stopped("INPUT_SIZE")
        bound = len(body) + 4096
        reserve = _cost(bound, MAX_OUTPUT)
        digest = sha256(body)
        with self.lock, self.db:
            if self.halted:
                raise Stopped(self.halted)
            old = self.db.execute("SELECT * FROM requests WHERE trial=?", (trial,)).fetchone()
            if old:
                if old["state"] != "settled" or old["body_sha"] != digest:
                    self.halted = "REPLAY_MISMATCH"
                    raise Stopped(self.halted)
                return old["content"], dict(
                    input=old["input"], output=old["output"], cost=old["cost"]
                )
            count, at_risk = self.db.execute(
                "SELECT count(*),COALESCE(sum(CASE WHEN state='settled' THEN cost "
                "ELSE reserve END),0) "
                "FROM requests"
            ).fetchone()
            if count >= MAX_CALLS or at_risk + reserve > MAX_MICROS:
                self.halted = "GRANT_LIMIT"
                raise Stopped(self.halted)
            request_id = str(uuid4())
            self.db.execute(
                "INSERT INTO requests(trial, request_id, body_sha, state, reserve, started) "
                "VALUES (?,?,?,'reserved',?,?)",
                (trial, request_id, digest, reserve, datetime.now(UTC).isoformat()),
            )
        response = {}
        raw = bytearray()
        error = None
        provider_id = None
        try:
            with self.client.stream(
                "POST",
                ENDPOINT,
                content=body,
                headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
            ) as resp:
                provider_id = resp.headers.get("x-request-id")
                for part in resp.iter_bytes():
                    if len(raw) + len(part) > MAX_RESPONSE_BYTES:
                        raise ValueError("Response too large")
                    raw.extend(part)
                if resp.status_code != 200:
                    error = f"PROVIDER_HTTP_{resp.status_code}"
                else:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        response = parsed
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            error = type(exc).__name__
        usage = response.get("usage")
        tokens = _token_usage(usage)
        choices = response.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        msg = choice.get("message")
        content = (
            msg.get("content")
            if isinstance(msg, dict) and choice.get("finish_reason") == "stop"
            else None
        )
        content = content if isinstance(content, str) else ""
        input_tokens, output_tokens = tokens or (0, 0)
        cost = _cost(input_tokens, output_tokens)
        if not tokens:
            error = error or "PROVIDER_USAGE_MISSING"
        elif cost > reserve or input_tokens > bound or output_tokens > MAX_OUTPUT:
            error = "PROVIDER_USAGE_ABOVE_RESERVATION"
        with self.lock, self.db:
            self.db.execute(
                "UPDATE requests SET state=?,cost=?,finished=?,input=?,output=?,content=?,usage=?,"
                "response_sha=?,provider_id=?,completion_id=?,model=?,finish=?,reason=? "
                "WHERE trial=?",
                (
                    "settled" if tokens else "unknown",
                    cost,
                    datetime.now(UTC).isoformat(),
                    input_tokens,
                    output_tokens,
                    content,
                    json.dumps(usage),
                    sha256(bytes(raw)) if raw else None,
                    provider_id,
                    response.get("id"),
                    response.get("model"),
                    choice.get("finish_reason"),
                    error,
                    trial,
                ),
            )
            if error:
                self.halted = error
        if error:
            raise Stopped(error)
        return content, dict(input=input_tokens, output=output_tokens, cost=cost)
