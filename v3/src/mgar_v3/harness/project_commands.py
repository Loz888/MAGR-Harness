"""H06 language adapters: prepare, test, build, start and readiness.

Adapters produce argv and parse real process output.  They never install
packages themselves: dependency preparation goes through the H04 broker port,
which currently denies every package source, so an unmet dependency is
reported as an actionable waiting state instead of being faked as a pass.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.harness.broker import IsolatedExecutionBroker
from mgar_v3.harness.commands import CommandSession, ReadinessProbe, ServiceLaunch
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionRequest,
    sha256_text,
)

ProjectKind = Literal["python", "node", "go", "unknown"]
TestStatus = Literal[
    "passed",
    "failed",
    "no_tests",
    "unverified",
    "timeout",
    "cancelled",
    "error",
    "waiting_approval",
]
DependencyState = Literal["ready", "not_required", "waiting_approval", "unsatisfiable"]


class ProjectProfile(Model):
    kind: ProjectKind
    root: str = ""
    test_argv: tuple[str, ...] = ()
    build_argv: tuple[str, ...] = ()
    start_argv: tuple[str, ...] = ()
    dependency_argv: tuple[str, ...] = ()
    dependency_files: tuple[str, ...] = ()
    readiness: ReadinessProbe | None = None
    notes: tuple[str, ...] = ()


class DependencyStatus(Model):
    status: DependencyState
    detail: str
    argv: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    action: str = ""


class TestReceipt(Model):
    receipt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    handle_id: str
    kind: ProjectKind
    argv: tuple[str, ...]
    status: TestStatus
    tests_run: Annotated[int, Field(ge=0)]
    tests_failed: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)]
    exit_code: int | None
    tree_hash: str
    log_sha256: str
    log_bytes: Annotated[int, Field(ge=0)]
    duration_seconds: Annotated[float, Field(ge=0)]
    detail: str = ""
    missing: tuple[str, ...] = ()
    action: str = ""


class BuildReceipt(Model):
    receipt_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    handle_id: str
    kind: ProjectKind
    argv: tuple[str, ...]
    status: Literal["passed", "failed", "timeout", "cancelled", "error"]
    exit_code: int | None
    tree_hash: str
    log_sha256: str
    log_bytes: Annotated[int, Field(ge=0)]
    duration_seconds: Annotated[float, Field(ge=0)]


def _has(files: Mapping[str, str], name: str) -> bool:
    return name in files


def _prefix(files: Mapping[str, str], prefix: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for path in files)


def detect_project(files: Mapping[str, str], *, root: str = "") -> ProjectProfile:
    """Choose one adapter from real repository files, without guessing a runtime."""

    if root:
        scoped = {
            path[len(root) + 1 :]: content
            for path, content in files.items()
            if path == root or path.startswith(root + "/")
        }
    else:
        scoped = dict(files)
    package = None
    if "package.json" in scoped:
        try:
            package = json.loads(scoped["package.json"])
        except ValueError:
            package = None
    if package is not None:
        scripts = package.get("scripts") if isinstance(package, dict) else None
        scripts = scripts if isinstance(scripts, dict) else {}
        test_argv = ("npm", "test", "--silent")
        build_argv = ("npm", "run", "build", "--silent") if "build" in scripts else ()
        start_argv = ("npm", "start", "--silent") if "start" in scripts else ()
        lock = "package-lock.json" in scoped or "pnpm-lock.yaml" in scoped
        dependency = (
            "pnpm install --frozen-lockfile"
            if "pnpm-lock.yaml" in scoped
            else ("npm ci --no-audit --no-fund" if lock else "npm install --no-audit --no-fund")
        )
        return ProjectProfile(
            kind="node",
            root=root,
            test_argv=test_argv,
            build_argv=build_argv,
            start_argv=start_argv,
            dependency_argv=tuple(dependency.split()),
            dependency_files=("package.json", "package-lock.json"),
            notes=("node readiness must be supplied by the caller",),
        )
    if _has(scoped, "go.mod"):
        return ProjectProfile(
            kind="go",
            root=root,
            test_argv=("go", "test", "-v", "-buildvcs=false", "./..."),
            build_argv=("go", "build", "-buildvcs=false", "./..."),
            start_argv=("go", "run", "."),
            dependency_argv=("go", "mod", "download"),
            dependency_files=("go.mod", "go.sum"),
        )
    python_files = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini")
    if any(_has(scoped, name) for name in python_files) or _prefix(scoped, "tests"):
        requirements = _has(scoped, "requirements.txt")
        has_pytest_config = _has(scoped, "pytest.ini") or "pytest" in scoped.get(
            "pyproject.toml", ""
        )
        return ProjectProfile(
            kind="python",
            root=root,
            test_argv=("python3", "-m", "pytest", "-q")
            if has_pytest_config
            else (
                "python3",
                "-m",
                "unittest",
                "discover",
                "-v",
            ),
            build_argv=("python3", "-m", "compileall", "-q", "."),
            dependency_argv=("python3", "-m", "pip", "install", "-r", "requirements.txt")
            if requirements
            else (),
            dependency_files=("requirements.txt", "pyproject.toml"),
            notes=("R03 image has no pytest; unittest uses the standard library",),
        )
    return ProjectProfile(kind="unknown", root=root)


_TEST_PATTERNS = {
    "pytest": re.compile(
        r"(?:(?P<passed>\d+) passed)|(?:(?P<failed>\d+) failed)|"
        r"(?:(?P<error>\d+) error)|(?:(?P<skipped>\d+) skipped)|"
        r"(?:(?P<xfailed>\d+) xfailed)|(?:(?P<xpassed>\d+) xpassed)"
    ),
    "unittest": re.compile(
        r"Ran (?P<ran>\d+) tests? in [\d.]+s(?:\s*\((?P<detail>[^)]*)\))?",
        re.MULTILINE,
    ),
    "go": re.compile(r"^(?:--- )?(?P<status>PASS|FAIL|SKIP):", re.MULTILINE),
    "node": re.compile(
        r"^# (?P<key>tests|pass|fail|skipped|skip|todo|cancelled) (?P<count>\d+)\s*$",
        re.MULTILINE,
    ),
}

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def infer_test_runner(argv: Sequence[str]) -> str:
    """Identify the actual trusted executable/reporter, not the business task kind."""
    if not argv:
        return "unknown"
    program = argv[0].rsplit("/", 1)[-1]
    joined = " ".join(argv)
    if program == "go":
        return "go"
    if program in {"pytest", "py.test", "mocha", "ava"}:
        return "pytest" if program == "py.test" else program
    if program in {"python", "python3"}:
        return "unittest" if "unittest" in joined else "pytest"
    if program in {"node", "npm", "npx", "pnpm"}:
        if "mocha" in joined:
            return "mocha"
        if "ava" in joined:
            return "ava"
        return "node"
    return "unknown"


def test_project_kind(argv: Sequence[str], fallback: str = "unknown") -> ProjectKind:
    runner = infer_test_runner(argv)
    if runner in {"pytest", "unittest"}:
        return "python"
    if runner in {"node", "mocha", "ava"}:
        return "node"
    if runner == "go":
        return "go"
    return fallback if fallback in {"python", "node", "go"} else "unknown"  # type: ignore[return-value]


test_project_kind.__test__ = False  # type: ignore[attr-defined]


def parse_tests(
    kind: ProjectKind, output: str, exit_code: int | None
) -> tuple[TestStatus, int, int, int, str]:
    """Return status, run, failed, skipped and a human detail.

    Exit 0 alone is never a pass: a run with no discovered tests, all skipped
    tests, or an unparsable count stays ``no_tests``/``unverified``.
    """

    output = _ANSI.sub("", output).replace("\r", "\n")
    if kind == "python" and "Ran " in output:
        match = _TEST_PATTERNS["unittest"].search(output)
        if match:
            ran = int(match.group("ran"))
            detail = match.group("detail") or ""
            summary = re.search(
                r"^(OK|FAILED)(?:\s*\(([^)]*)\))?\s*$", output, re.MULTILINE,
            )
            if summary is None:
                return "unverified", ran, 0, 0, "unittest produced no terminal summary"
            detail = summary.group(2) or detail
            failed = 0
            skipped = 0
            for part in detail.split(","):
                key, _, value = part.strip().partition("=")
                if key in {"failures", "errors"} and value.isdigit():
                    failed += int(value)
                if key == "skipped" and value.isdigit():
                    skipped = int(value)
            if ran == 0:
                return "no_tests", 0, 0, 0, "unittest discovered no tests"
            if skipped >= ran:
                return "no_tests", ran, 0, skipped, "every discovered test was skipped"
            if exit_code == 0 and failed == 0 and summary.group(1) == "OK":
                return "passed", ran, 0, skipped, f"{ran - skipped} tests passed; {skipped} skipped"
            return "failed", ran, failed, skipped, f"{failed} failures/errors"
    if kind == "python":
        counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0,
                  "xfailed": 0, "xpassed": 0}
        summary_lines = [
            line for line in output.splitlines() if _TEST_PATTERNS["pytest"].search(line)
        ]
        pytest_summary = summary_lines[-1] if summary_lines else ""
        for match in _TEST_PATTERNS["pytest"].finditer(pytest_summary):
            for key in counts:
                value = match.group(key)
                if value:
                    counts[key] += int(value)
        total = sum(counts.values())
        if exit_code == 5 or "no tests ran" in output:
            return "no_tests", 0, 0, 0, "pytest collected no tests"
        if total == 0:
            return "unverified", 0, 0, 0, "pytest produced no parsable test count"
        failed = counts["failed"] + counts["error"]
        # The existing receipt has one excluded-outcome counter, as with AVA's
        # known failures. Expected failures are never successful assertions.
        skipped = counts["skipped"] + counts["xfailed"]
        detail = (
            f"{counts['passed']} tests passed; {failed} failures/errors; "
            f"{counts['skipped']} skipped; {counts['xfailed']} xfailed; "
            f"{counts['xpassed']} xpassed"
        )
        if skipped >= total:
            detail = "every test was skipped or an expected failure; " + detail
            return "no_tests", total, 0, skipped, detail
        if exit_code == 0 and failed == 0:
            return "passed", total, 0, skipped, detail
        return "failed", total, failed, skipped, detail
    if kind == "go":
        passed = len(re.findall(r"^(?:--- )?PASS:", output, re.MULTILINE))
        failed = len(re.findall(r"^(?:--- )?FAIL:", output, re.MULTILINE))
        skipped = len(re.findall(r"^(?:--- )?SKIP:", output, re.MULTILINE))
        total = passed + failed + skipped
        if total == 0 and "no test files" in output:
            return "no_tests", total, failed, skipped, "go reported no test files"
        if total == 0:
            return "unverified", 0, 0, 0, "go output has no executed test results"
        if skipped >= total:
            return "no_tests", total, 0, skipped, "every test was skipped"
        if exit_code == 0 and failed == 0:
            return "passed", total, 0, skipped, f"{passed} tests passed; {skipped} skipped"
        return "failed", total, failed, skipped, f"{failed} failures"
    if kind == "node":
        values = {"tests": 0, "pass": 0, "fail": 0, "skip": 0, "todo": 0, "cancelled": 0}
        for match in _TEST_PATTERNS["node"].finditer(output):
            key = "skip" if match.group("key") == "skipped" else match.group("key")
            values[key] = int(match.group("count"))
        values["skip"] += values["todo"]
        values["fail"] += values["cancelled"]
        total = values["tests"] or values["pass"] + values["fail"] + values["skip"]
        reporter = "TAP"
        if total and values["pass"] + values["fail"] + values["skip"] != total:
            return "unverified", total, values["fail"], values["skip"], "TAP counts are incomplete"
        if total == 0:
            mocha = {"passing": 0, "failing": 0, "pending": 0}
            for match in re.finditer(
                r"^\s*(\d+)\s+(passing|failing|pending)(?:\s+\([^\n]*\))?\s*$",
                output, re.MULTILINE,
            ):
                mocha[match.group(2)] = int(match.group(1))
            values = {"tests": sum(mocha.values()), "pass": mocha["passing"],
                      "fail": mocha["failing"], "skip": mocha["pending"]}
            total, reporter = values["tests"], "Mocha"
        if total == 0:
            ava = {"passed": 0, "failed": 0, "skipped": 0, "todo": 0}
            ava_summaries = list(re.finditer(
                r"^\s*(\d+)\s+tests?\s+(passed|failed|skipped|todo)\s*$",
                output, re.MULTILINE,
            ))
            for match in ava_summaries:
                ava[match.group(2)] = int(match.group(1))
            if ava_summaries and not any(m.group(2) == "passed" for m in ava_summaries):
                ava["passed"] = len(re.findall(
                    r"^\s*[✔√]\s+(?!\[expected fail\])\S.*$", output, re.MULTILINE,
                ))
            known_failures = re.findall(
                r"^\s*(\d+)\s+known failures?\s*$", output, re.MULTILINE,
            )
            if known_failures:
                ava["skipped"] += int(known_failures[-1])
            values = {"tests": sum(ava.values()), "pass": ava["passed"],
                      "fail": ava["failed"], "skip": ava["skipped"] + ava["todo"]}
            total, reporter = values["tests"], "AVA"
        if total == 0:
            return "unverified", 0, 0, 0, "Node output has no TAP, Mocha or AVA test summary"
        if values["skip"] >= total:
            return "no_tests", total, 0, values["skip"], "every test was skipped"
        if exit_code == 0 and values["fail"] == 0:
            detail = f"{reporter}: {values['pass']} tests passed"
            if values["skip"]:
                detail += f"; {values['skip']} skipped/pending/known failures"
            return "passed", total, 0, values["skip"], detail
        return (
            "failed", total, values["fail"], values["skip"],
            f"{reporter}: {values['fail']} failures",
        )
    return "unverified", 0, 0, 0, "no adapter recognized the output"


def _read_all(session: CommandSession, handle_id: str, *, cap: int = 1_048_576) -> str:
    pages: list[str] = []
    offset = 0
    while len("".join(pages)) < cap:
        page = session.read_log(handle_id, offset=offset)
        pages.append(page.data)
        offset = page.next_offset
        if page.eof or not page.data:
            break
    return "".join(pages)[:cap]


def _request(
    broker: IsolatedExecutionBroker,
    argv: Sequence[str],
    *,
    root: str,
    purpose: Literal["test", "build", "command"],
    timeout: float,
) -> ExecutionRequest:
    return ExecutionRequest(
        operation_id="h06-" + uuid.uuid4().hex,
        purpose=purpose,
        argv=tuple(argv),
        cwd=root,
        timeout_seconds=timeout,
        execution_epoch=broker.authority.current().execution_epoch,
    )


def run_tests(
    broker: IsolatedExecutionBroker,
    session: CommandSession,
    profile: ProjectProfile,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
    *,
    timeout: float = 120,
) -> TestReceipt:
    started = time.monotonic()
    waiting = _test_readiness(broker, profile, grant, cancellation)
    if waiting is not None:
        waiting_payload = {
            "kind": profile.kind,
            "argv": list(profile.test_argv),
            "status": waiting.status,
            "missing": list(waiting.missing),
        }
        return TestReceipt(
            receipt_id=digest(waiting_payload),
            handle_id="",
            kind=profile.kind,
            argv=profile.test_argv,
            status="waiting_approval",
            tests_run=0,
            tests_failed=0,
            skipped=0,
            exit_code=None,
            tree_hash=broker.workspace_tree_hash(),
            log_sha256=sha256_text(""),
            log_bytes=0,
            duration_seconds=time.monotonic() - started,
            detail=waiting.detail,
            missing=waiting.missing,
            action=waiting.action,
        )
    request = _request(
        broker, profile.test_argv, root=profile.root, purpose="test", timeout=timeout
    )
    handle = session.run(request, grant, cancellation, purpose="test")
    final = session.wait(handle.handle_id, timeout=timeout + 5)
    output = _read_all(session, handle.handle_id)
    if final.state == "timed_out":
        status, run, failed, skipped, detail = "timeout", 0, 0, 0, "test command timed out"
    elif final.state == "cancelled":
        status, run, failed, skipped, detail = "cancelled", 0, 0, 0, "test command was cancelled"
    elif final.state == "failed" and final.exit_code is None:
        status, run, failed, skipped, detail = "error", 0, 0, 0, "test container disappeared"
    else:
        status, run, failed, skipped, detail = parse_tests(profile.kind, output, final.exit_code)
    duration = time.monotonic() - started
    payload = {
        "handle": handle.handle_id,
        "argv": list(profile.test_argv),
        "status": status,
        "run": run,
        "failed": failed,
        "skipped": skipped,
        "exit": final.exit_code,
        "tree": final.tree_hash,
        "log": sha256_text(output),
    }
    return TestReceipt(
        receipt_id=digest(payload),
        handle_id=handle.handle_id,
        kind=profile.kind,
        argv=profile.test_argv,
        status=status,
        tests_run=run,
        tests_failed=failed,
        skipped=skipped,
        exit_code=final.exit_code,
        tree_hash=final.tree_hash,
        log_sha256=sha256_text(output),
        log_bytes=len(output.encode("utf-8")),
        duration_seconds=duration,
        detail=detail,
    )


def run_build(
    broker: IsolatedExecutionBroker,
    session: CommandSession,
    profile: ProjectProfile,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
    *,
    timeout: float = 120,
) -> BuildReceipt:
    if not profile.build_argv:
        raise DomainError("HARNESS_BUILD_UNAVAILABLE", "Project has no detected build command", 400)
    started = time.monotonic()
    request = _request(
        broker, profile.build_argv, root=profile.root, purpose="build", timeout=timeout
    )
    handle = session.run(request, grant, cancellation, purpose="build")
    final = session.wait(handle.handle_id, timeout=timeout + 5)
    output = _read_all(session, handle.handle_id)
    if final.state == "timed_out":
        status: Literal["passed", "failed", "timeout", "cancelled", "error"] = "timeout"
    elif final.state == "cancelled":
        status = "cancelled"
    elif final.state == "exited" and final.exit_code == 0:
        status = "passed"
    elif final.state == "failed" and final.exit_code is None:
        status = "error"
    else:
        status = "failed"
    payload = {
        "handle": handle.handle_id,
        "argv": list(profile.build_argv),
        "status": status,
        "exit": final.exit_code,
        "tree": final.tree_hash,
        "log": sha256_text(output),
    }
    return BuildReceipt(
        receipt_id=digest(payload),
        handle_id=handle.handle_id,
        kind=profile.kind,
        argv=profile.build_argv,
        status=status,
        exit_code=final.exit_code,
        tree_hash=final.tree_hash,
        log_sha256=sha256_text(output),
        log_bytes=len(output.encode("utf-8")),
        duration_seconds=time.monotonic() - started,
    )


def prepare_dependencies(
    broker: IsolatedExecutionBroker,
    profile: ProjectProfile,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
) -> DependencyStatus:
    """Check the runtime, then ask the H04 dependency port, never install directly."""

    if not profile.dependency_argv and not (
        profile.kind == "python" and "pytest" in profile.test_argv
    ):
        return DependencyStatus(status="not_required", detail="project declares no dependencies")
    runtime = {
        "python": ("python3", "--version"),
        "node": ("node", "--version"),
        "go": ("go", "version"),
    }
    probe = runtime.get(profile.kind)
    if probe:
        available, detail = _tool_available(broker, grant, cancellation, probe)
        if not available:
            missing = profile.kind if profile.kind != "unknown" else "runtime"
            return DependencyStatus(
                status="waiting_approval",
                detail=f"{profile.kind} runtime is unavailable: {detail}",
                argv=profile.dependency_argv,
                missing=(missing,),
                action=(
                    "Provide an approved image that contains the runtime, then rerun "
                    "dependency preparation"
                ),
            )
    if profile.kind == "python" and "pytest" in profile.test_argv:
        available, detail = _tool_available(
            broker, grant, cancellation, ("python3", "-c", "import pytest")
        )
        if not available:
            return DependencyStatus(
                status="waiting_approval",
                detail=f"pytest is not installed in the pinned image: {detail}",
                argv=profile.dependency_argv or ("python3", "-m", "pip", "install", "pytest"),
                missing=("pytest",),
                action=(
                    "Approve a dependency source phase for package installation, or use "
                    "the standard-library unittest adapter"
                ),
            )
    try:
        broker.prepare_dependencies({"argv": list(profile.dependency_argv)}, grant, cancellation)
    except DomainError as exc:
        return DependencyStatus(
            status="waiting_approval",
            detail=str(exc),
            argv=profile.dependency_argv,
            missing=tuple(profile.dependency_files),
            action=(
                "Dependency installation needs a separately approved package source; "
                "H04 currently denies network egress"
            ),
        )
    return DependencyStatus(
        status="ready", detail="dependencies prepared by the broker", argv=profile.dependency_argv
    )


def _test_readiness(
    broker: IsolatedExecutionBroker,
    profile: ProjectProfile,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
) -> DependencyStatus | None:
    """Report an unavailable test runtime as a waiting state, not a fake result.

    Running a test command whose runtime or package cannot exist in the pinned
    image would otherwise produce an ``unverified`` receipt from a process that
    never ran a test.  Checking first keeps that case in the approval workflow.
    """

    if profile.kind == "python" and "pytest" in profile.test_argv:
        available, detail = _tool_available(
            broker, grant, cancellation, ("python3", "-c", "import pytest")
        )
        if not available:
            return DependencyStatus(
                status="waiting_approval",
                detail=f"pytest is not installed in the pinned image: {detail}",
                argv=profile.test_argv,
                missing=("pytest",),
                action=(
                    "Approve a dependency source phase for package installation, or use "
                    "the standard-library unittest adapter"
                ),
            )
    if profile.kind == "node":
        available, detail = _tool_available(broker, grant, cancellation, ("node", "--version"))
        if not available:
            return DependencyStatus(
                status="waiting_approval",
                detail=f"node runtime is unavailable: {detail}",
                argv=profile.test_argv,
                missing=("node",),
                action=(
                    "Provide an approved image that contains the runtime, then rerun "
                    "dependency preparation"
                ),
            )
    return None


def _tool_available(
    broker: IsolatedExecutionBroker,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
    argv: Sequence[str],
) -> tuple[bool, str]:
    request = _request(broker, argv, root="", purpose="command", timeout=15)
    try:
        receipt = broker.execute(request, grant, cancellation)
    except DomainError as exc:
        return False, str(exc)
    if receipt.exit_code == 0:
        return True, receipt.stdout[-200:]
    return False, (receipt.stdout or receipt.stderr)[-300:]


def start_application(
    broker: IsolatedExecutionBroker,
    session: CommandSession,
    profile: ProjectProfile,
    grant: CapabilityGrant,
    cancellation: CancellationToken,
) -> tuple[ServiceLaunch, str, bool]:
    """Start a detected application and wait for readiness.

    Returns the service launch, its address and whether that address is
    reachable from the host.  H04 containers have ``--network=none`` and no
    published ports, so the returned value is always container-local.
    """

    if not profile.start_argv or profile.readiness is None:
        raise DomainError(
            "HARNESS_START_UNAVAILABLE",
            "Project has no detected start command or readiness probe",
            400,
        )
    request = _request(
        broker, profile.start_argv, root=profile.root, purpose="command", timeout=300
    )
    service = session.start_service(request, grant, cancellation, profile.readiness)
    return service, service.address, service.host_reachable


def project_files(root: Path) -> dict[str, str]:
    """Read a bounded set of project manifests for adapter detection."""

    files: dict[str, str] = {}
    for name in (
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "go.mod",
        "go.sum",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "pytest.ini",
    ):
        path = root / name
        if path.is_file():
            try:
                files[name] = path.read_text(encoding="utf-8", errors="replace")[:200_000]
            except OSError:
                continue
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if entry.is_dir() and not entry.is_symlink():
            files[f"{entry.name}/"] = ""
    return files
