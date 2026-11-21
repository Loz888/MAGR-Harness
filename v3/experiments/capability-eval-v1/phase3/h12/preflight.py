"""Zero-model readiness matrix over pin-verified, offline H06 command execution."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from mgar_v3.domain import DomainError
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest

PY_GO_IMAGE = "sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6"
NODE_IMAGE = "sha256:b0544b84d0593cab876785c43df426ffe552508b1c4af873c8df3cdf93cf6fd6"
SUITE = (
    "import pathlib\nimport examples.demo\n"
    "assert examples.demo.VALUE == 11\n"
    "assert pathlib.Path('settings.txt').read_text() == 'stable'\n"
    "assert pathlib.Path('test-data/golden.txt').read_text() == 'contract'\n"
)


def verify(root: Path, fault: str, repaired=False) -> dict:
    command, version_args, exact = ("python3", ("--version",), "Python 3.11.2")
    image = PY_GO_IMAGE
    if fault.startswith("node-"):
        command, version_args, exact = ("node", ("--version",), "v18.10.0")
        image = NODE_IMAGE
    elif fault.startswith("go-"):
        command, version_args, exact = ("go", ("version",), "go version go1.27.1 linux/amd64")
    approved = (command, "python3", "missing-phase3-command")
    lease = issue_lease(
        mode="investigate",
        actor="owner",
        workspace_id=uuid4().hex,
        repository="fixture/h12-r09",
        execution_epoch=1,
        allowed_commands=tuple(dict.fromkeys(approved)),
        timeout_seconds=20,
    )
    grant = CapabilityGrant(
        actor=lease.actor,
        workspace_id=lease.workspace_id,
        repository=lease.repository,
        read_paths=("",),
        write_paths=(),
        execute=True,
        allowed_commands=lease.allowed_commands,
        max_seconds=20,
    )
    broker = IsolatedExecutionBroker(root, lease, image_id=image)
    records = []

    def run(argv, cwd="."):
        req = ExecutionRequest(
            operation_id="preflight-" + uuid4().hex,
            purpose="test",
            argv=argv,
            cwd=cwd,
            execution_epoch=1,
            timeout_seconds=15,
        )
        try:
            r = broker.execute(req, grant, CancellationToken())
        except DomainError as exc:
            records.append({"argv": argv, "cwd": cwd, "domain_code": exc.code})
            return None
        records.append(r.model_dump(mode="json"))
        return r

    def result(code):
        return {
            "category": code,
            "status": "READY" if code == "READY" else "BLOCKED",
            "agent_quality": "NOT_RUN",
            "actual_model_calls": 0,
            "image": image,
            "receipts": records,
        }

    wanted = (
        "runtime-version-absent-from-locked-image"
        if fault.endswith("-version") and not repaired
        else exact
    )
    cwd = "unavailable-directory" if fault == "wrong-directory" and not repaired else "."
    executable = (
        "missing-phase3-command" if fault == "missing-command" and not repaired else command
    )
    probe = run((executable, *version_args), cwd)
    if cwd != "." and (probe is None or probe.exit_code):
        return result("WORKING_DIRECTORY")
    if executable != command and (probe is None or probe.exit_code):
        return result("COMMAND_NOT_FOUND")
    if probe is None or probe.exit_code:
        return result("INFRASTRUCTURE")
    if probe.stdout.strip() != wanted:
        return result("RUNTIME_VERSION")
    suite = run(("python3", "-B", "check_suite.py"))
    if suite is None:
        return result("INFRASTRUCTURE")
    if suite.exit_code == 0:
        return result("READY")
    logs = suite.stdout + suite.stderr
    if "ModuleNotFoundError" in logs and "examples" in logs:
        return result("MISSING_PACKAGE")
    if "FileNotFoundError" in logs and "settings.txt" in logs:
        return result("MISSING_FILE")
    if "FileNotFoundError" in logs and "test-data/golden.txt" in logs:
        return result("TEST_ASSET_MISSING")
    return result("BASELINE_FAILURE")


def inputs(root, fault, *, repaired):
    root.mkdir()
    files = {
        "check_suite.py": SUITE,
        "settings.txt": "stable",
        "test-data/golden.txt": "contract",
        "examples/__init__.py": "",
        "examples/demo.py": "VALUE = 11\n",
    }
    for name, content in files.items():
        if not repaired and (
            (fault == "missing-file" and name == "settings.txt")
            or (fault == "test-asset" and name.startswith("test-data/"))
            or (fault in {"missing-package", "examples-package"} and name.startswith("examples/"))
        ):
            continue
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
