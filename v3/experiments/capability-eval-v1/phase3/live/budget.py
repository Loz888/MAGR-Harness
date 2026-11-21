"""A single reserve-before-send journal for the separately capped live studies."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from mgar_v3.domain import DomainError
from mgar_v3.harness.product_models import ProductDeepSeekChatAdapter, product_request_input_bound

LIMITS = {"h12": (256, 5_000_000), "h17": (1440, 24_000_000)}
TRIALS = {"h12": 32, "h17": 180}
CALLS_PER_TRIAL = 8
OUTPUT_CAPS = {"h12": 2048, "h17": 4096}
MAX_INPUT = 192_000
URL = "https://api.deepseek.com/chat/completions"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def stamp():
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path: Path, frozen: str, previous: tuple[str, ...] = ()):
        self.guard = path.with_suffix(".lock").open("a")
        fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS manifest (sha256 TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS calls (
              study TEXT NOT NULL, trial TEXT NOT NULL, operation TEXT PRIMARY KEY,
              state TEXT NOT NULL, reserved INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0,
              started TEXT NOT NULL, ended TEXT, request_sha256 TEXT NOT NULL,
              receipt TEXT, model_input_upper INTEGER NOT NULL,
              model_output_limit INTEGER NOT NULL);
        """)
        old = [r[0] for r in self.db.execute("SELECT sha256 FROM manifest")]
        busy = self.db.execute("SELECT 1 FROM calls WHERE state != 'settled'").fetchone()
        if old and set(old) != {frozen}:
            if not previous or not set(old) <= {frozen, *previous} or busy:
                raise ValueError("Ledger belongs to another frozen grant")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO manifest VALUES (?)", (frozen,))
        self.halted = bool(
            self.db.execute("SELECT 1 FROM calls WHERE state != 'settled'").fetchone()
        )

    def close(self):
        self.db.close()
        self.guard.close()

    def used(self, trial):
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM calls WHERE trial=?", (trial,)).fetchone())

    def check_admission(self, study, trial, request):
        limit_in = product_request_input_bound(request) + 4096
        if limit_in > MAX_INPUT:
            raise DomainError("PHASE3_INPUT_BOUND", "Frozen request cannot fit input bound", 409)
        bound = (limit_in * 3 + request.max_output_tokens * 12 + 9) // 10
        max_calls, max_micros = LIMITS[study]
        with self.lock:
            if self.halted:
                raise DomainError("PHASE3_USAGE_UNKNOWN", "Unknown provider usage", 409)
            calls, exposure = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN state='settled' THEN settled "
                "ELSE reserved END),0) FROM calls WHERE study=?",
                (study,),
            ).fetchone()
            sent = self.db.execute(
                "SELECT COUNT(*) FROM calls WHERE trial=?",
                (trial,),
            ).fetchone()[0]
            if calls >= max_calls or exposure + bound > max_micros or sent >= CALLS_PER_TRIAL:
                raise DomainError("PHASE3_BUDGET", "Frozen study grant is exhausted", 409)

    def reserve(self, study, trial, operation, limit_in, limit_out, digest):
        upper = (limit_in * 3 + limit_out * 12 + 9) // 10
        max_calls, max_micros = LIMITS[study]
        with self.lock, self.db:
            if self.halted:
                raise DomainError(
                    "PHASE3_USAGE_UNKNOWN", "Experiment stopped at an unknown receipt", 409
                )
            count, spent = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN state='settled' THEN settled "
                "ELSE reserved END),0) FROM calls WHERE study=?",
                (study,),
            ).fetchone()
            per_trial = self.db.execute(
                "SELECT COUNT(*) FROM calls WHERE trial=?", (trial,)
            ).fetchone()[0]
            if count >= max_calls or per_trial >= CALLS_PER_TRIAL or spent + upper > max_micros:
                raise DomainError("PHASE3_BUDGET", "Frozen experiment budget exhausted", 409)
            self.db.execute(
                "INSERT INTO calls (study, trial, operation, state, reserved, started, "
                "request_sha256, model_input_upper, model_output_limit) VALUES (?,?,?,?,?,?,?,?,?)",
                (study, trial, operation, "reserved", upper, stamp(), digest, limit_in, limit_out),
            )
        return upper

    def finish(self, op, response, max_in, max_out):
        receipt = response.output_receipt
        usage = response.usage
        known = usage.known and 0 < usage.input_tokens <= max_in and usage.output_tokens <= max_out
        amount = (usage.input_tokens * 3 + usage.output_tokens * 12 + 9) // 10
        with self.lock, self.db:
            reserved = self.db.execute(
                "SELECT reserved FROM calls WHERE operation=?", (op,)
            ).fetchone()[0]
            known = known and reserved >= amount
            self.db.execute(
                "UPDATE calls SET state=?, settled=?, ended=?, receipt=? WHERE operation=?",
                (
                    "settled" if known else "unknown",
                    amount if known else 0,
                    stamp(),
                    canonical(
                        {
                            "usage": usage.model_dump(mode="json"),
                            "finish": response.finish_reason,
                            "output_receipt": receipt.model_dump(mode="json") if receipt else None,
                            "error_code": response.error_code,
                        }
                    ),
                    op,
                ),
            )
            self.halted |= not known
        return known

    def lost(self, operation, category):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE calls SET state='unknown', ended=?, receipt=? WHERE operation=?",
                (stamp(), canonical({"local_error": category}), operation),
            )
            self.halted = True


class BoundedAdapter(ProductDeepSeekChatAdapter):
    def __init__(self, *, ledger, study, trial, arm, **kwargs):
        super().__init__(endpoint=URL, max_attempts=1, **kwargs)
        self.ledger, self.study, self.trial, self.arm = ledger, study, trial, arm

    def complete(self, request, cancellation):
        cancellation.ensure_active()
        cap = request.max_output_tokens
        if cap > OUTPUT_CAPS[self.study]:
            raise DomainError("PHASE3_OUTPUT_SCOPE", "Trial budget not bound before dispatch", 409)
        self._validate(request)
        body = self._body(request)
        if body.get("model") != "deepseek-flash" or body.get("thinking") != {"type": "enabled"}:
            raise DomainError("PHASE3_MODEL_BINDING", "Model identity or thinking differs", 409)
        if body.get("reasoning_effort") != "max":
            raise DomainError("PHASE3_MODEL_BINDING", "Reasoning effort differs", 409)
        wire = canonical(body).encode()
        input_bound = max(product_request_input_bound(request), len(wire) + 4096)
        if input_bound > MAX_INPUT:
            raise DomainError(
                "PHASE3_INPUT_BOUND", "Request exceeds the frozen experiment bound", 409
            )
        self.ledger.reserve(
            self.study,
            self.trial,
            request.operation_id,
            input_bound,
            cap,
            hashlib.sha256(wire).hexdigest(),
        )
        try:
            result = super().complete(request, cancellation)
        except BaseException as exc:
            self.ledger.lost(request.operation_id, type(exc).__name__)
            raise
        if not self.ledger.finish(request.operation_id, result, input_bound, cap):
            return result.model_copy(
                update={"usage": result.usage.model_copy(update={"known": False})}
            )
        return result
