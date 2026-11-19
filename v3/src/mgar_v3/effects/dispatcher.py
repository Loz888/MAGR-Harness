from typing import Protocol
from uuid import UUID

import httpx

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError
from mgar_v3.general_agent.actions import ActionIntent, ActionMode, ActionReceipt


class DefinitiveFailure(Exception):
    """The remote service definitively rejected the action without creating an object."""


class Transport(Protocol):
    mode: str

    def send(self, action: Row) -> str: ...

    def find(self, action: Row) -> str | None: ...


class Dispatcher:
    def __init__(self, engine: Engine, transport: Transport):
        self.engine = engine
        self.transport = transport

    def tick(self) -> UUID | None:
        action = self.engine.claim_effect(
            self.transport.mode,
            getattr(self.transport, "action_types", ("issue_comment",)),
            getattr(self.transport, "profile_hash", None),
        )
        if action is None:
            return None
        action_id = UUID(str(action["action_id"]))
        lease = UUID(str(action["lease_token"]))
        try:
            external_id = self.transport.send(action)
        except DefinitiveFailure:
            self.engine.effect_result(action_id, lease, failed=True)
        except (httpx.HTTPError, OSError, TimeoutError):
            self.engine.effect_result(action_id, lease, unknown=True)
        except Exception:
            self.engine.effect_result(action_id, lease, unknown=True)
            raise
        else:
            self.engine.effect_result(action_id, lease, external_id=external_id)
        return action_id

    def reconcile(self, action_id: UUID) -> bool:
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if run["execution_mode"] != self.transport.mode:
                raise DomainError(
                    "EXECUTION_MODE_MISMATCH", "Reconciliation transport mode differs"
                )
            if action["action_type"] not in getattr(
                self.transport, "action_types", ("issue_comment",)
            ):
                raise DomainError(
                    "TRANSPORT_TYPE_MISMATCH", "Transport cannot reconcile this action"
                )
        # No business transaction remains open during a remote lookup.
        external_id = self.transport.find(action)
        return self.engine.reconcile_effect(action_id, external_id)


class PublicationTransport(Protocol):
    mode: str

    def send(self, action: Row) -> str: ...

    def find(self, action: Row) -> str | None: ...


class PublicationJournal(Protocol):
    """Engine-backed action/outbox journal used by publication dispatchers."""

    def claim_publication(self, execution_mode: str) -> Row | None: ...

    def publication(self, publication_id: UUID) -> Row: ...

    def publication_result(
        self,
        publication_id: UUID,
        lease_token: UUID,
        *,
        external_id: str | None = None,
        unknown: bool = False,
        failed: bool = False,
        reason_code: str = "",
        reason: str = "",
    ) -> str: ...

    def reconcile_publication(self, publication_id: UUID, external_id: str | None) -> bool: ...


class PublicationDispatcher:
    """Leased dispatcher for Workbench Draft PR publication (V3.5 R04).

    The Web request only enqueues; this dispatcher owns the remote write.
    A definitive rejection is recorded as failed, an unknown outcome is
    recorded as unknown and never retried, and reconciliation is read-only.
    """

    def __init__(self, engine: PublicationJournal, transport: PublicationTransport):
        self.engine = engine
        self.transport = transport

    def tick(self) -> UUID | None:
        action = self.engine.claim_publication(self.transport.mode)
        if action is None:
            return None
        publication_id = UUID(str(action["publication_id"]))
        lease = UUID(str(action["lease_token"]))
        try:
            external_id = self.transport.send(action)
        except DefinitiveFailure as exc:
            self.engine.publication_result(
                publication_id,
                lease,
                failed=True,
                reason_code=str(getattr(exc, "code", "DEFINITIVE_REJECTION")),
                reason=str(exc),
            )
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            self.engine.publication_result(
                publication_id,
                lease,
                unknown=True,
                reason_code="OUTCOME_UNKNOWN",
                reason=f"Remote result is unknown: {type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            self.engine.publication_result(
                publication_id,
                lease,
                unknown=True,
                reason_code="OUTCOME_UNKNOWN",
                reason=f"Remote result is unknown: {type(exc).__name__}: {exc}",
            )
            raise
        else:
            self.engine.publication_result(publication_id, lease, external_id=external_id)
        return publication_id

    def reconcile(self, publication_id: UUID) -> bool:
        action = self.engine.publication(publication_id)
        if action["execution_mode"] != self.transport.mode:
            raise DomainError("EXECUTION_MODE_MISMATCH", "Reconciliation transport mode differs")
        # No business transaction remains open during the read-only lookup.
        external_id = self.transport.find(action)
        return self.engine.reconcile_publication(publication_id, external_id)


class GeneralActionTransport(Protocol):
    """An injected account-bound provider adapter.

    Send applies the intent's idempotency key and version precondition at the
    provider. Find performs a read-only lookup on the original key.
    """

    mode: ActionMode
    provider_id: str

    def observe(self, intent: ActionIntent) -> str | None: ...

    def send(self, intent: ActionIntent, payload: bytes | None) -> ActionReceipt: ...

    def find(self, intent: ActionIntent) -> ActionReceipt | None: ...


class GeneralActionDispatcher:
    """Dispatch approved universal actions with the original provider idempotency key."""

    def __init__(self, engine: Engine, transport: GeneralActionTransport) -> None:
        self.engine = engine
        self.transport = transport

    def tick(self) -> UUID | None:
        journal = self.engine.general_actions
        intent = journal.candidate(self.transport.mode, self.transport.provider_id)
        if intent is None:
            return None
        payload = None
        if intent.payload_sha256 is not None:
            try:
                payload = self.engine.artifacts.read(intent.payload_sha256)
            except DomainError as exc:
                if exc.code not in {"ARTIFACT_MISSING", "ARTIFACT_CORRUPTED"}:
                    raise
                if journal.fail_payload(intent.action_id, intent.payload_sha256, exc.code):
                    return intent.action_id
                return None
        observed = self.transport.observe(intent) if intent.precondition_sha256 else None
        attempt = journal.claim(intent.action_id, observed)
        if attempt is None:
            return None
        try:
            receipt = self.transport.send(intent, payload)
            journal.result(intent.action_id, attempt, receipt=receipt)
        except DefinitiveFailure:
            journal.result(intent.action_id, attempt, definitive_failure=True)
        except (httpx.HTTPError, OSError, TimeoutError):
            journal.result(intent.action_id, attempt)
        except Exception:
            journal.result(intent.action_id, attempt)
            raise
        return intent.action_id

    def reconcile(self, action_id: UUID) -> bool:
        journal = self.engine.general_actions
        action = journal.reconcilable(action_id, self.transport.mode, self.transport.provider_id)
        receipt = self.transport.find(action.intent)
        if receipt is None:
            return False
        assert action.attempt_id is not None
        result = journal.result(action_id, action.attempt_id, receipt=receipt)
        return result.status == "succeeded"
