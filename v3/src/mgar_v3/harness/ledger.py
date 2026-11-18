"""BudgetLedger adapter backed by the Engine's existing PostgreSQL budgets."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from mgar_v3.domain import DomainError
from mgar_v3.harness.contracts import (
    BudgetLimits,
    BudgetReservation,
    BudgetSnapshot,
    OperationKind,
    ReservationStatus,
)
from mgar_v3.harness.store import HarnessStore


class PersistentBudgetLedger:
    def __init__(self, store: HarnessStore, task_id: str, epoch: int) -> None:
        self.store, self.task_id, self.epoch = store, task_id, epoch
        self.limits = BudgetLimits.model_validate(store.get(task_id)["limits"])

    def _check_task(self, task_id: str) -> None:
        if task_id != self.task_id:
            raise DomainError("HARNESS_SCOPE_DENIED", "Budget belongs to another task", 403)

    def reserve(
        self, task_id: str, operation_id: str, kind: OperationKind, *,
        reserved_micros: int = 0, reserved_units: int = 1,
    ) -> BudgetReservation:
        self._check_task(task_id)
        if reserved_micros < 0 or reserved_units < 1:
            raise ValueError("Reservation amounts must be nonnegative and units positive")
        if kind == "tool" and reserved_micros:
            raise DomainError("HARNESS_BUDGET_DENIED", "Tool reservations have no model charge")
        with self.store.engine.transaction() as conn:
            row = self.store._guard(conn, task_id, self.epoch)
            self.store._unsealed(row)
            existing = conn.execute(
                "SELECT * FROM app.harness_operations WHERE run_id=%s AND operation_id=%s "
                "FOR UPDATE", (row["run_id"], operation_id),
            ).fetchone()
            if existing and (
                existing["budget_kind"] != kind or existing["reserved_units"] != reserved_units
            ):
                raise DomainError("IDEMPOTENCY_CONFLICT", "Reservation input changed", 409)
            if existing and existing["state"] != "released":
                if existing["state"] == "unknown":
                    raise DomainError("RECOVERY_BLOCKED", "Unknown reservation cannot be resent")
                result = self._reservation(conn, existing)
                if result.reserved_micros != reserved_micros:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Reserved amount changed", 409)
                if kind == "model" and existing["state"] != "known":
                    self.store._group_known(conn, row)
                return result
            count = conn.execute(
                "SELECT coalesce(sum(CASE WHEN state='known' THEN actual_units "
                "WHEN state IN ('prepared','dispatched','unknown') THEN reserved_units "
                "ELSE 0 END),0) AS calls FROM app.harness_operations "
                "WHERE run_id=%s AND budget_kind=%s", (row["run_id"], kind),
            ).fetchone()
            cap = self.limits.max_model_calls if kind == "model" else self.limits.max_tool_calls
            if count and count["calls"] + reserved_units > cap:
                raise DomainError("BUDGET_EXCEEDED", "Per-task call budget exhausted", 429)
            call_id = None
            if kind == "model":
                self.store._group_known(conn, row)
                reserved = conn.execute(
                    "UPDATE app.authorization_budgets SET reserved_micros=reserved_micros+%s,"
                    "used_calls=used_calls+%s WHERE profile_hash=%s "
                    "AND used_calls+%s<=call_limit "
                    "AND reserved_micros+settled_micros+%s<=limit_micros RETURNING profile_hash",
                    (reserved_micros, reserved_units, row["profile_hash"], reserved_units,
                     reserved_micros),
                ).fetchone()
                local = conn.execute(
                    "UPDATE app.budgets SET reserved_micros=reserved_micros+%s "
                    "WHERE mandate_id=%s AND reserved_micros+settled_micros+%s<=limit_micros "
                    "RETURNING mandate_id",
                    (reserved_micros, row["mandate_id"], reserved_micros),
                ).fetchone()
                if reserved is None or local is None:
                    raise DomainError("BUDGET_EXCEEDED", "Model-call budget exhausted", 429)
                call_id = uuid4()
                conn.execute(
                    "INSERT INTO app.usage_calls(call_id,run_id,step_key,status,reserved_micros,"
                    "profile_hash) VALUES (%s,%s,%s,'reserved',%s,%s)",
                    (call_id, row["run_id"], operation_id, reserved_micros, row["profile_hash"]),
                )
            if existing:
                operation = conn.execute(
                    "UPDATE app.harness_operations SET state='prepared',execution_epoch=%s,"
                    "model_call_id=%s,reserved_units=%s,actual_units=0,reason='',"
                    "request_hash=NULL,intent='{}',receipt='{}',result_hash=NULL,updated_at=now() "
                    "WHERE reservation_id=%s RETURNING *",
                    (self.epoch, call_id, reserved_units, existing["reservation_id"]),
                ).fetchone()
            else:
                operation = conn.execute(
                    "INSERT INTO app.harness_operations(reservation_id,run_id,operation_id,"
                    "execution_epoch,kind,budget_kind,state,model_call_id,reserved_units) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'prepared',%s,%s) RETURNING *",
                    (uuid4(), row["run_id"], operation_id, self.epoch, kind, kind, call_id,
                     reserved_units),
                ).fetchone()
            assert operation is not None
            self.store.engine._event(conn, row["run_id"], "harness_budget_reserved",
                                     operation_id=operation_id, operation_kind=kind)
            return self._reservation(conn, operation)

    def _operation(self, conn: Any, reservation_id: str) -> dict[str, Any]:
        row = self.store._guard(conn, self.task_id, self.epoch)
        operation = conn.execute(
            "SELECT * FROM app.harness_operations WHERE reservation_id=%s AND run_id=%s "
            "FOR UPDATE", (UUID(reservation_id), row["run_id"]),
        ).fetchone()
        if operation is None or operation["budget_kind"] is None:
            raise DomainError("CALL_NOT_FOUND", "Budget reservation was not found", 404)
        return cast(dict[str, Any], operation)

    def _reservation(self, conn: Any, operation: dict[str, Any]) -> BudgetReservation:
        call = None
        if operation["model_call_id"] is not None:
            call = conn.execute(
                "SELECT * FROM app.usage_calls WHERE call_id=%s", (operation["model_call_id"],)
            ).fetchone()
        state = operation["state"]
        status = "settled" if state == "known" else "reserved" if state in {
            "prepared", "dispatched"
        } else state
        return BudgetReservation(
            reservation_id=str(operation["reservation_id"]), task_id=self.task_id,
            operation_id=operation["operation_id"],
            kind=cast(OperationKind, operation["budget_kind"]),
            reserved_micros=int(call["reserved_micros"]) if call else 0,
            reserved_units=operation["reserved_units"], status=cast(ReservationStatus, status),
            actual_micros=int(call["settled_micros"] or 0) if call else 0,
            actual_units=operation["actual_units"], reason=operation["reason"],
        )

    def get(self, reservation_id: str) -> BudgetReservation:
        with self.store.engine.transaction() as conn:
            return self._reservation(conn, self._operation(conn, reservation_id))

    def settle(
        self, reservation_id: str, *, actual_micros: int, actual_units: int = 1,
    ) -> BudgetReservation:
        if actual_micros < 0 or actual_units < 0:
            raise ValueError("Settlement amounts cannot be negative")
        with self.store.engine.transaction() as conn:
            operation = self._operation(conn, reservation_id)
            previous = self._reservation(conn, operation)
            if operation["budget_kind"] == "model":
                if previous.status != "settled":
                    raise DomainError(
                        "MODEL_RESULT_REQUIRED", "Persist response and usage atomically first"
                    )
                if (previous.actual_micros, previous.actual_units) != (actual_micros, actual_units):
                    raise DomainError("SETTLEMENT_CONFLICT", "Known settlement changed", 409)
                return previous
            if actual_micros or actual_units > operation["reserved_units"]:
                raise DomainError("SETTLEMENT_CONFLICT", "Tool settlement exceeds reservation")
            if operation["state"] in {"unknown", "released"}:
                raise DomainError("RECOVERY_BLOCKED", "Tool outcome requires reconciliation")
            if operation["state"] == "known" and previous.actual_units != actual_units:
                raise DomainError("SETTLEMENT_CONFLICT", "Known tool settlement changed", 409)
            conn.execute(
                "UPDATE app.harness_operations SET state='known',actual_units=%s,updated_at=now() "
                "WHERE reservation_id=%s", (actual_units, operation["reservation_id"]),
            )
            operation.update(state="known", actual_units=actual_units)
            return self._reservation(conn, operation)

    def mark_unknown(self, reservation_id: str, reason: str) -> BudgetReservation:
        with self.store.engine.transaction() as conn:
            operation = self._operation(conn, reservation_id)
            if operation["state"] == "known":
                return self._reservation(conn, operation)
            if operation["model_call_id"] is not None:
                self.store.engine.settle_call(operation["model_call_id"], None)
            conn.execute(
                "UPDATE app.harness_operations SET state='unknown',reason=%s,updated_at=now() "
                "WHERE reservation_id=%s", (reason[:500], operation["reservation_id"]),
            )
            operation.update(state="unknown", reason=reason[:500])
            return self._reservation(conn, operation)

    def release(self, reservation_id: str) -> BudgetReservation:
        with self.store.engine.transaction() as conn:
            operation = self._operation(conn, reservation_id)
            if operation["state"] == "released":
                return self._reservation(conn, operation)
            if operation["state"] != "prepared":
                raise DomainError("RECOVERY_BLOCKED", "Sent operation cannot be refunded", 409)
            if operation["model_call_id"] is not None:
                reservation = self._reservation(conn, operation)
                row = self.store._guard(conn, self.task_id, self.epoch)
                conn.execute(
                    "UPDATE app.budgets SET reserved_micros=reserved_micros-%s "
                    "WHERE mandate_id=%s", (reservation.reserved_micros, row["mandate_id"]),
                )
                conn.execute(
                    "UPDATE app.authorization_budgets SET reserved_micros=reserved_micros-%s,"
                    "used_calls=used_calls-%s WHERE profile_hash=%s",
                    (reservation.reserved_micros, reservation.reserved_units, row["profile_hash"]),
                )
                call_id = operation["model_call_id"]
                conn.execute(
                    "UPDATE app.harness_operations SET model_call_id=NULL WHERE reservation_id=%s",
                    (operation["reservation_id"],),
                )
                conn.execute("DELETE FROM app.usage_calls WHERE call_id=%s", (call_id,))
            conn.execute(
                "UPDATE app.harness_operations SET state='released',actual_units=0,"
                "updated_at=now() "
                "WHERE reservation_id=%s", (operation["reservation_id"],),
            )
            operation.update(state="released", actual_units=0, model_call_id=None)
            return self._reservation(conn, operation)

    def snapshot(self, task_id: str) -> BudgetSnapshot:
        self._check_task(task_id)
        with self.store.engine.transaction() as conn:
            row = self.store._row(conn, task_id)
            count = conn.execute(
                "SELECT budget_kind,coalesce(sum(CASE WHEN state='known' THEN actual_units "
                "WHEN state IN ('prepared','dispatched','unknown') THEN reserved_units "
                "ELSE 0 END),0) AS calls FROM app.harness_operations WHERE run_id=%s "
                "AND budget_kind IS NOT NULL GROUP BY budget_kind", (row["run_id"],),
            ).fetchall()
            counts = {entry["budget_kind"]: int(entry["calls"]) for entry in count}
            money = conn.execute(
                "SELECT reserved_micros+settled_micros AS cost FROM app.budgets "
                "WHERE mandate_id=%s", (row["mandate_id"],),
            ).fetchone()
            assert money is not None
            return BudgetSnapshot(task_id=task_id, model_calls=counts.get("model", 0),
                                  tool_calls=counts.get("tool", 0), cost_micros=money["cost"])

    def resource_snapshot(self, task_id: str) -> dict[str, Any]:
        """Read existing task/shared authority; never reserve or reset resources."""
        self._check_task(task_id)
        with self.store.engine.transaction() as conn:
            row = self.store._row(conn, task_id, self.epoch)
            used = self.snapshot(task_id)
            local = conn.execute(
                "SELECT limit_micros,reserved_micros,settled_micros FROM app.budgets "
                "WHERE mandate_id=%s", (row["mandate_id"],),
            ).fetchone()
            shared = conn.execute(
                "SELECT limit_micros,reserved_micros,settled_micros,call_limit,used_calls "
                "FROM app.authorization_budgets WHERE profile_hash=%s", (row["profile_hash"],),
            ).fetchone()
            unknown = conn.execute(
                "SELECT count(*) AS calls FROM app.usage_calls u WHERE u.profile_hash=%s "
                "AND (u.status='unknown' OR EXISTS (SELECT 1 FROM app.harness_operations o "
                "WHERE o.model_call_id=u.call_id AND o.state='unknown'))", (row["profile_hash"],),
            ).fetchone()
            tokens = conn.execute(
                "SELECT coalesce(sum(coalesce((usage->>'input_tokens')::bigint,"
                "(usage->>'prompt_tokens')::bigint,0)),0) AS input_tokens,"
                "coalesce(sum(coalesce((usage->>'output_tokens')::bigint,"
                "(usage->>'completion_tokens')::bigint,0)),0) AS output_tokens "
                "FROM app.usage_calls WHERE run_id=%s", (row["run_id"],),
            ).fetchone()
            assert local is not None and shared is not None and unknown is not None
            limits = BudgetLimits.model_validate(row["limits"]).model_dump(mode="json")
            limits["max_cost_micros"] = min(limits["max_cost_micros"], local["limit_micros"])
            return {
                "limits": limits, "used": used.model_dump(mode="json"),
                "reserved_micros": local["reserved_micros"],
                "unknown_calls": int(unknown["calls"]),
                "shared": {**dict(shared), "unknown_calls": int(unknown["calls"])},
                "tokens": {key: int(value) for key, value in (tokens or {}).items()},
                "deadline_at": row["deadline_at"].isoformat(),
            }
