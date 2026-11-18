"""Content-address a fixed public structural checker from its declared specification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mgar_v3.harness.public_probe_types import PublicProbePlan
from mgar_v3.harness.semantic_types import SharedHelperSpec

PROGRAM = Path(__file__).parents[1] / "harness/assets/shared_helper_probe.py"


def structural_program(spec: SharedHelperSpec) -> tuple[bytes, PublicProbePlan]:
    plan_id = (
        "r06-shared-helper-" + hashlib.sha256(spec.model_dump_json().encode()).hexdigest()[:20]
    )
    # JSON is passed as data, never interpolated as Python source expressions.
    serialized = json.dumps(spec.model_dump(mode="json"), sort_keys=True)
    raw = (
        PROGRAM.read_bytes()
        + b"\nmain(json.loads("
        + repr(serialized).encode()
        + b"), "
        + repr(plan_id).encode()
        + b", "
        + repr(
            (PROGRAM.parent / "shared_return_probe.go").read_text()
            if spec.language == "go"
            else None
        ).encode()
        + b")\n"
    )
    plan = PublicProbePlan(
        plan_id=plan_id,
        asset_sha256=hashlib.sha256(raw).hexdigest(),
        case_ids=(spec.acceptance_requirement_id,),
    )
    return raw, plan


def freeze_structural_probe(spec: SharedHelperSpec, root: Path) -> PublicProbePlan:
    raw, plan = structural_program(spec)
    target = root / plan.asset_sha256 / "probe.py"
    if target.exists():
        if target.is_symlink() or target.read_bytes() != raw:
            raise ValueError("Existing structural public asset differs")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    return plan
