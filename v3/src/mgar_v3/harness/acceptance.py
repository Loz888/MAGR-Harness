"""R01 immutable public acceptance contracts. These records never grant tool authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, ValidationError, model_validator

from mgar_v3.domain import DomainError, Model, digest

if TYPE_CHECKING:
    from mgar_v3.harness.contracts import TaskBinding

HASH = r"^[a-f0-9]{64}$"


class AcceptanceScope(Model):
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=250)
    read_paths: tuple[str, ...] | None = None
    write_paths: tuple[str, ...] | None = None
    execute: bool | None = None
    network: bool | None = None
    allowed_commands: tuple[str, ...] | None = None


class ContractCheck(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,60}$")
    kind: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=1024)
    required: Literal[True]
    requirement_id: str | None = None
    evidence_check_ids: tuple[str, ...] = ()


class AcceptanceBinding(Model):
    """An integrity receipt, not a statement that every natural-language clause passed."""

    stage: Literal["planner", "worker", "context", "verifier", "scorer"]
    acceptance_id: str = Field(min_length=1, max_length=100)
    acceptance_revision: int = Field(ge=1)
    acceptance_hash: str = Field(pattern=HASH)
    required_ids: tuple[str, ...] = Field(min_length=1)
    check_evidence: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class AcceptanceContract(Model):
    schema_version: Literal["harness-acceptance-v1"] = "harness-acceptance-v1"
    acceptance_id: str = Field(min_length=1, max_length=100)
    acceptance_revision: int = Field(ge=1, le=256)
    acceptance_hash: str = Field(pattern=HASH)
    catalog_revision: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=16000)
    required_checks: tuple[ContractCheck, ...] = Field(min_length=1, max_length=96)
    required_artifacts: tuple[str, ...] = Field(min_length=1, max_length=8)
    scope: AcceptanceScope
    baseline_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    task_type: str = Field(min_length=1, max_length=40)
    verification_requirements_hash: str = Field(pattern=HASH)
    previous_hash: str | None = Field(default=None, pattern=HASH)
    change_reason: str = Field(min_length=1, max_length=1024)
    affected_check_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def integrity(self) -> Self:
        ids = self.required_ids
        if (
            len(ids) != len(set(ids))
            or len(self.required_artifacts) != len(set(self.required_artifacts))
            or not set(self.affected_check_ids).issubset(ids)
            or (self.acceptance_revision == 1) != (self.previous_hash is None)
            or self.acceptance_hash != digest(self.model_dump(mode="json",
                                                              exclude={"acceptance_hash"}))
        ):
            raise ValueError("Acceptance contract hash, revision or required IDs differ")
        mechanical = {c.id for c in self.required_checks if c.kind != "public_requirement"}
        if not mechanical or any(
            not c.evidence_check_ids or not set(c.evidence_check_ids).issubset(mechanical)
            for c in self.required_checks if c.kind == "public_requirement"
        ):
            raise ValueError("Public clauses must retain explicit existing check associations")
        return self

    @property
    def required_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.required_checks)

    def assert_task(self, task: TaskBinding) -> None:
        try:
            # model_copy(update=...) does not run Pydantic validators.
            AcceptanceContract.model_validate(self.model_dump(mode="json"))
            if (
                self.acceptance_id != contract_id(task)
                or self.objective != task.objective
                or self.task_type != task.task_kind
                or self.catalog_revision != task.acceptance_revision
                or self.baseline_commit != (task.acceptance_baseline_commit or task.base_commit)
                or self.scope != task_scope(task)
                or self.verification_requirements_hash != requirements_hash(task)
                or self.required_artifacts != required_artifacts(task)
                or tuple(c.description for c in self.required_checks
                         if c.kind == "public_requirement") != task.public_requirements
            ):
                raise ValueError("Task differs from its acceptance contract")
        except (ValueError, ValidationError) as exc:
            raise DomainError("ACCEPTANCE_CONTRACT_MISMATCH",
                              "Current acceptance authority differs", 409) from exc

    def assert_checklist(self, items: Sequence[Mapping[str, Any]]) -> None:
        by_id = {i["item_id"]: i for i in items}
        if len(by_id) != len(items):
            raise DomainError("ACCEPTANCE_CONTRACT_MISMATCH", "Checklist IDs are duplicated", 409)
        expected_ids = {c.id for c in self.required_checks if c.kind != "public_requirement"}
        if {i["item_id"] for i in items if i.get("required") is True} != expected_ids:
            raise DomainError("ACCEPTANCE_REQUIRED_CHANGED",
                              "Required checklist and contract IDs differ", 409)
        for check in self.required_checks:
            if check.kind == "public_requirement":
                continue
            item = by_id.get(check.id)
            if item is None or any(
                item.get(key) != expected for key, expected in (
                    ("required", True), ("kind", check.kind), ("description", check.description),
                    ("requirement_id", check.requirement_id),
                )
            ):
                raise DomainError("ACCEPTANCE_REQUIRED_CHANGED",
                                  "A required acceptance item was removed or weakened", 409)

    def binding(
        self, stage: Any, evidence: Mapping[str, Sequence[str]] | None = None
    ) -> AcceptanceBinding:
        links = {key: tuple(value) for key, value in (evidence or {}).items()}
        for check in self.required_checks:
            if check.kind == "public_requirement":
                links[check.id] = tuple(dict.fromkeys(
                    item for key in check.evidence_check_ids for item in links.get(key, ())
                ))
        return AcceptanceBinding(
            stage=stage, acceptance_id=self.acceptance_id,
            acceptance_revision=self.acceptance_revision, acceptance_hash=self.acceptance_hash,
            required_ids=self.required_ids, check_evidence=links,
        )

    def assert_binding(self, binding: AcceptanceBinding | None, stage: str) -> None:
        if binding is None or binding.model_dump(exclude={"check_evidence"}) != (
            self.binding(stage).model_dump(exclude={"check_evidence"})
        ):
            raise DomainError("ACCEPTANCE_BINDING_MISMATCH",
                              "Missing or stale acceptance integrity receipt", 409)


def contract_id(task: TaskBinding) -> str:
    return str(uuid5(NAMESPACE_URL, f"harness:{task.run_id}:{task.task_id}:acceptance"))


def task_scope(task: TaskBinding) -> AcceptanceScope:
    return task.acceptance_scope or AcceptanceScope(
        workspace_id=task.workspace_id, repository=task.repository,
    )


def requirements_hash(task: TaskBinding) -> str:
    return digest([r.model_dump(mode="json") for r in task.requirements])


def required_artifacts(task: TaskBinding) -> tuple[str, ...]:
    return ("verification_report",) + (
        ("evidence_references",) if task.task_kind == "inspect" else ("patch",)
    ) + (("test_receipts",) if task.requirements and task.task_kind != "inspect" else ()) + (
        ("process_receipts",) if task.task_kind == "run_app" else ()
    )


def contract_for(
    task: TaskBinding,
    items: Sequence[Mapping[str, Any]],
    *,
    previous: AcceptanceContract | None = None,
    reason: str = "Initial trusted task and public requirements",
) -> AcceptanceContract:
    checks = tuple(ContractCheck(
        id=i["item_id"], kind=i["kind"], description=i["description"],
        required=True, requirement_id=i.get("requirement_id"),
    ) for i in items if i["required"])
    ids = tuple(c.id for c in checks)
    checks += tuple(ContractCheck(
        id="public." + digest(text)[:16], kind="public_requirement",
        description=text, required=True, evidence_check_ids=ids,
    ) for text in task.public_requirements)
    if previous is not None:
        current = {c.id: c for c in checks}
        if any(current.get(c.id) != c for c in previous.required_checks
               if c.kind != "public_requirement"):
            raise DomainError("ACCEPTANCE_REQUIRED_CHANGED",
                              "Revision cannot weaken existing required checks", 409)
        if not {c.description for c in previous.required_checks
                if c.kind == "public_requirement"}.issubset(task.public_requirements):
            raise DomainError("ACCEPTANCE_REQUIRED_CHANGED",
                              "Revision cannot drop existing public requirements", 409)
    data = {
        "schema_version": "harness-acceptance-v1", "acceptance_id": contract_id(task),
        "acceptance_revision": previous.acceptance_revision + 1 if previous else 1,
        "catalog_revision": task.acceptance_revision, "objective": task.objective,
        "required_checks": [c.model_dump(mode="json") for c in checks],
        "required_artifacts": list(required_artifacts(task)),
        "scope": task_scope(task).model_dump(mode="json"),
        "baseline_commit": task.acceptance_baseline_commit or task.base_commit,
        "task_type": task.task_kind, "verification_requirements_hash": requirements_hash(task),
        "previous_hash": previous.acceptance_hash if previous else None,
        "change_reason": reason,
        "affected_check_ids": [c.id for c in checks],
    }
    return AcceptanceContract.model_validate({**data, "acceptance_hash": digest(data)})


def score_binding(
    result: Mapping[str, Any], profile: Any, public_task: Mapping[str, Any],
) -> AcceptanceBinding:
    """Verify new results only. Historical legacy score files remain unchanged."""
    from mgar_v3.harness.coding_loop import CodingLoop
    from mgar_v3.harness.contracts import TaskBinding, VerificationReceipt

    try:
        contract = AcceptanceContract.model_validate(result["acceptance_contract"])
        task = TaskBinding.model_validate(result["acceptance_task"])
        contract.assert_task(task)
        contract.assert_checklist(result["checklist"]["items"])
        expected = {i.item_id: i for i in CodingLoop._base_items(
            task, getattr(profile, "lifecycle_policy", None)
        ) if i.required}
        actual = {c.id: c for c in contract.required_checks}
        if any(
            key not in actual
            or (actual[key].kind, actual[key].description, actual[key].requirement_id)
            != (item.kind, item.description, item.requirement_id)
            for key, item in expected.items()
        ):
            raise ValueError("Scorer required policy differs")
        if (
            contract.objective != profile.objective
            or contract.task_type != profile.task_kind
            or contract.catalog_revision != profile.acceptance_revision
            or contract.baseline_commit != public_task["base_commit"]
            or tuple(c.description for c in contract.required_checks
                     if c.kind == "public_requirement") != profile.public_requirements
            or contract.scope.read_paths != profile.read_paths
            or contract.scope.write_paths != profile.write_paths
            or str(task.run_id) != result["run_id"]
            or task.task_id != result["task_id"]
        ):
            raise ValueError("Runtime acceptance differs from the frozen public profile")
        bindings = [AcceptanceBinding.model_validate(r) for r in result["acceptance_receipts"]]
        by_stage = {b.stage: b for b in bindings}
        if len(bindings) != len(by_stage) or set(by_stage) != {
            "planner", "context", "worker", "verifier",
        }:
            raise ValueError("Missing, unexpected or duplicate stage binding")
        for stage in ("planner", "context", "worker", "verifier"):
            contract.assert_binding(by_stage.get(stage), stage)
        verification = VerificationReceipt.model_validate(result["verification"])
        contract.assert_binding(verification.acceptance_binding, "verifier")
        contract.assert_binding(AcceptanceBinding.model_validate(
            result["acceptance_binding"]), "worker")
        if result["status"] == "completed" and (
            verification.status != "verified"
            or verification.tree_hash != result["tree_after"]
            or verification.task_id != task.task_id
            or set(result["acceptance_artifacts"]) != set(contract.required_artifacts)
            or any(not result["acceptance_artifacts"][k] for k in contract.required_artifacts)
        ):
            raise ValueError("Required current verification/artifacts are unavailable")
        assert verification.acceptance_binding is not None
        return contract.binding("scorer", verification.acceptance_binding.check_evidence)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise DomainError("ACCEPTANCE_SCORER_MISMATCH",
                          "Scorer cannot verify the complete acceptance contract", 409) from exc
