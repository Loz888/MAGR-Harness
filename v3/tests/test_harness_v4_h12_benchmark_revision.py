"""New benchmark corrections cannot silently mutate another source or H00."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from mgar_v3.quality_eval.h12_benchmark import (
    apply_public_amendments,
    public_baseline_contract_matches,
)
from mgar_v3.quality_eval.h12_v2_freeze import public_assets_for_task
from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
from mgar_v3.quality_eval.harness_v4 import CatalogError


def test_independent_public_asset_is_hash_bound_and_scoped(tmp_path: Path) -> None:
    relative = '.local/h12/independent-holdout-v2-20261113/public-assets/test.py'
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b'public test')
    task = {
        'split': 'holdout',
        'public_assets': [
            {'source': relative, 'target': 'tests/test_public.py', 'sha256': _sha(b'public test')}
        ],
    }
    assert public_assets_for_task(tmp_path, task) == [('tests/test_public.py', b'public test')]
    source.write_bytes(b'changed')
    with pytest.raises(CatalogError, match='hash'):
        public_assets_for_task(tmp_path, task)
    source.write_bytes(b'public test')
    task['public_assets'][0]['target'] = '../src/code.py'
    with pytest.raises(CatalogError, match='path'):
        public_assets_for_task(tmp_path, task)


def test_v2_registry_uses_original_for_unchanged_dev_tasks(tmp_path: Path) -> None:
    new = tmp_path / 'new-dev.json'
    held = tmp_path / 'holdout.json'
    new.write_bytes(b'new dev')
    held.write_bytes(b'private holdout')
    runner = object.__new__(BenchmarkV2Runner)
    runner._dev_replacement_id = 'new-dev'
    runner.freeze = {'assets': {'dev_judges': new, 'holdout_judges': held}}
    assert runner._registry_for({'split': 'dev', 'task_id': 'old-dev'}) == (None, None)
    assert runner._registry_for({'split': 'dev', 'task_id': 'new-dev'}) == (
        new, _sha(b'new dev')
    )
    assert runner._registry_for({'split': 'holdout', 'task_id': 'new-held'}) == (
        held, _sha(b'private holdout')
    )


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fixture(tmp_path: Path):
    workspace = tmp_path / "copy"
    (workspace / "test").mkdir(parents=True)
    files = []
    for name in ("a.ts", "b.ts"):
        (workspace / "test" / name).write_bytes(b"original")
        files.append(
            {
                "path": "test/" + name,
                "before_sha256": _sha(b"original"),
                "after_sha256": _sha(b"revised"),
                "replacements": [{"before": "original", "after": "revised"}],
            }
        )
    document = {
        "schema": "h12-public-test-amendment-v1",
        "revision": "h12-benchmark-v2",
        "repository": "example/project",
        "base_commit": "a" * 40,
        "files": files,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    return workspace, path, document


def _apply(workspace: Path, manifest: Path):
    return apply_public_amendments(
        workspace,
        manifest,
        _sha(manifest.read_bytes()),
        repository="example/project",
        commit="a" * 40,
    )


def test_applies_bound_public_tests_once_and_retains_receipt(tmp_path: Path) -> None:
    workspace, manifest, _ = _fixture(tmp_path)
    receipt = _apply(workspace, manifest)
    assert len(receipt["files"]) == 2
    assert (workspace / "test/a.ts").read_bytes() == b"revised"
    with pytest.raises(CatalogError, match="preimage"):
        _apply(workspace, manifest)


def test_last_invalid_preimage_cannot_partially_modify_first_file(tmp_path: Path) -> None:
    workspace, manifest, _ = _fixture(tmp_path)
    (workspace / "test/b.ts").write_bytes(b"other-author")
    with pytest.raises(CatalogError, match="preimage"):
        _apply(workspace, manifest)
    assert (workspace / "test/a.ts").read_bytes() == b"original"
    assert (workspace / "test/b.ts").read_bytes() == b"other-author"


@pytest.mark.parametrize(
    "field,value", [("repository", "other/project"), ("base_commit", "b" * 40), ("revision", "h00")]
)
def test_rejects_wrong_source_binding(tmp_path: Path, field: str, value: str) -> None:
    workspace, manifest, document = _fixture(tmp_path)
    document[field] = value
    manifest.write_text(json.dumps(document))
    with pytest.raises(CatalogError, match="binding"):
        _apply(workspace, manifest)


@pytest.mark.parametrize(
    "target", ["../test/a.ts", "/test/a.ts", "src/code.py", "test/../../a.ts", "test\\a.ts"]
)
def test_scope_cannot_expand_to_source_or_escape(tmp_path: Path, target: str) -> None:
    workspace, manifest, document = _fixture(tmp_path)
    document["files"][0]["path"] = target
    manifest.write_text(json.dumps(document))
    with pytest.raises(CatalogError, match="public test"):
        _apply(workspace, manifest)


def test_symlink_cannot_rewrite_external_file(tmp_path: Path) -> None:
    workspace, manifest, _ = _fixture(tmp_path)
    external = tmp_path / "sentinel"
    external.write_bytes(b"original")
    target = workspace / "test/a.ts"
    target.unlink()
    target.symlink_to(external)
    with pytest.raises(CatalogError, match="symlink"):
        _apply(workspace, manifest)
    assert external.read_bytes() == b"original"


def test_manifest_and_output_hashes_are_enforced(tmp_path: Path) -> None:
    workspace, manifest, document = _fixture(tmp_path)
    with pytest.raises(CatalogError, match="manifest"):
        apply_public_amendments(
            workspace, manifest, "0" * 64, repository="example/project", commit="a" * 40
        )
    document["files"][1]["after_sha256"] = "0" * 64
    manifest.write_text(json.dumps(document))
    with pytest.raises(CatalogError, match="result"):
        _apply(workspace, manifest)
    assert (workspace / "test/a.ts").read_bytes() == b"original"


def test_draft_readiness_is_separate_and_cannot_grant_paid_dispatch(tmp_path: Path) -> None:
    from mgar_v3.quality_eval.h12_readiness import execution_readiness
    from mgar_v3.quality_eval.harness_v4 import repo_root

    report = execution_readiness(
        {},
        tmp_path / "matrix",
        environment_revision="h12-dev-environment-v4",
        draft_proposal=repo_root() / "experiments/harness-v4/benchmark-v2/proposal.json",
    )
    assert report["status"] == "NOT_RUN"
    assert report["denominator"] == 12
    assert report["paid_dispatch_allowed"] is False
    assert report["paid_model_calls"] == 0
    ids = {row["task_id"] for row in report["tasks"]}
    assert "hv4-v2-dev-requests-quoted-content-type" in ids
    assert "hv4-dev-requests-content-type" not in ids


def test_draft_rejects_silent_removal_of_another_task(tmp_path: Path) -> None:
    from mgar_v3.quality_eval.h12_readiness import execution_readiness
    from mgar_v3.quality_eval.harness_v4 import repo_root

    source = repo_root() / "experiments/harness-v4/benchmark-v2"
    target = tmp_path / "proposal"
    shutil.copytree(source, target)
    catalog = target / "dev.jsonl"
    rows = [json.loads(line) for line in catalog.read_text().splitlines()]
    rows[-1] = json.loads(
        (repo_root() / "experiments/harness-v4/tasks/dev.jsonl").read_text().splitlines()[2]
    )
    catalog.write_text("".join(json.dumps(row) + "\n" for row in rows))
    proposal = target / "proposal.json"
    data = json.loads(proposal.read_text())
    data["dev_catalog_sha256"] = _sha(catalog.read_bytes())
    proposal.write_text(json.dumps(data))
    with pytest.raises(CatalogError, match="eleven"):
        execution_readiness(
            {},
            tmp_path / "matrix",
            environment_revision="h12-dev-environment-v4",
            draft_proposal=proposal,
        )


@pytest.mark.parametrize("invalid", [None, "pin", "asset", "unrelated", "incomplete"])
def test_explicit_red_baseline_requires_source_assets_and_actual_assertions(
    tmp_path: Path,
    invalid: str | None,
) -> None:
    task = {"repository": "example/project", "base_commit": "a" * 40, "judge_sha256": "b" * 64}
    contract = {
        **task,
        "schema": "h12-public-red-baseline-v2",
        "assets": {"tests/test_public.py": "c" * 64},
    }
    actual = {"tests/test_public.py": "c" * 64}
    target = "tests/other.py" if invalid == "unrelated" else "tests/test_public.py"
    raw = f"FAILED {target}::test_contract - AssertionError\n1 failed in 0.01s\n".encode()
    log = tmp_path / "public.log"
    log.write_bytes(raw)
    observed = {
        "status": "PASS",
        "baseline_test_status": "FAIL",
        "receipts": [
            {
                "private_log": str(log),
                "log_sha256": _sha(raw),
                "exit_code": 1,
                "state": "failed",
                "complete": invalid != "incomplete",
            }
        ],
    }
    if invalid == "pin":
        task["base_commit"] = "d" * 40
    if invalid == "asset":
        actual["tests/test_public.py"] = "e" * 64
    assert public_baseline_contract_matches(contract, task, actual, observed) is (invalid is None)


def test_v2_live_runner_rejects_the_reviewed_draft_before_engine_dispatch(tmp_path: Path) -> None:
    """Even an authorized live flag cannot turn the prepared draft into a paid run."""
    from types import SimpleNamespace

    from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
    from mgar_v3.quality_eval.harness_v4 import repo_root

    proposal = repo_root() / "experiments/harness-v4/benchmark-v2/proposal.json"
    with pytest.raises(CatalogError, match="independently frozen"):
        BenchmarkV2Runner(
            SimpleNamespace(harness=None),
            experiment_id="v2-must-not-start",
            evidence=tmp_path / "evidence",
            freeze_path=proposal,
            freeze_sha256=_sha(proposal.read_bytes()),
            api_key="test-not-a-real-provider-key",
            execute_live=True,
        )
    assert not (tmp_path / "evidence" / "candidate").exists()


@pytest.mark.parametrize("complete", [False, True])
def test_v2_holdout_live_gate_rejects_incomplete_or_below_threshold_dev(
    tmp_path: Path, complete: bool
) -> None:
    """The new independent split cannot spend before a frozen dev result qualifies."""
    from mgar_v3.domain import DomainError
    from mgar_v3.quality_eval.h12_v2_runner import BenchmarkV2Runner
    from mgar_v3.quality_eval.harness_v4 import load_jsonl, repo_root

    tasks = load_jsonl(repo_root() / "experiments/harness-v4/benchmark-v2/dev.jsonl")
    runner = object.__new__(BenchmarkV2Runner)
    runner.execute_live = True
    runner.evidence = tmp_path
    runner.freeze = {"tasks": {"dev": tasks}}
    runner._validate_frozen_execution = lambda: {}  # type: ignore[method-assign]
    if complete:
        for task in tasks:
            path = tmp_path / "candidate/dev" / task["task_id"] / "score.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(runner._not_run(task, "candidate", "synthetic")))
    with pytest.raises(DomainError) as caught:
        runner._live_policy("holdout")
    assert caught.value.code == "H12_DEV_THRESHOLD_REQUIRED"
