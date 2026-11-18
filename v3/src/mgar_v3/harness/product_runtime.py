"""Product-facing project discovery and execution over the existing H04/H06 ports.

The normal conversational entry uses this module explicitly. Frozen evaluation
profiles retain their original command registry and image selection. Project
metadata proposes commands; the server freezes the plan before issuing a lease.
No project command is executed on the host and no package network is enabled.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import Field

from mgar_v3.domain import DomainError, Model
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.commands import CommandSession, ReadinessProbe, ServiceLaunch
from mgar_v3.harness.context_tools import artifact_trailer
from mgar_v3.harness.contracts import (
    CancellationToken,
    CapabilityGrant,
    ExecutionReceipt,
    ExecutionRequest,
    HarnessToolCall,
    HarnessToolResult,
    ToolContext,
    ToolSpec,
    canonical_hash,
    sha256_text,
)
from mgar_v3.harness.project_commands import detect_project
from mgar_v3.harness.service import CodingToolRegistry, _execution_status, _test_feedback
from mgar_v3.harness.workspace import _read_regular_bytes, _scan


class ProductCommand(Model):
    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    cwd: str = ""
    purpose: Literal["test", "build", "command"] = "command"
    description: str = ""


class ProductRuntimePlan(Model):
    schema_version: Literal["harness-product-runtime-v1"] = "harness-product-runtime-v1"
    kind: Literal["python", "node", "go", "static", "unknown"] = "unknown"
    root: str = ""
    commands: dict[str, ProductCommand] = Field(default_factory=dict)
    start_argv: tuple[str, ...] = ()
    readiness: ReadinessProbe | None = None
    dependency_files: tuple[str, ...] = ()
    image_lock: str = ""
    verification_scope: Literal["tests", "build_only"] = "tests"

    @property
    def test_argv(self) -> tuple[str, ...]:
        command = self.commands.get("tests")
        return command.argv if command is not None else ()

    @property
    def programs(self) -> tuple[str, ...]:
        commands = [command.argv for command in self.commands.values()]
        if self.start_argv:
            commands.append(self.start_argv)
        return tuple(sorted({Path(argv[0]).name for argv in commands}))


def discover_product_plan(
    files: Mapping[str, str], *, image_lock: str = ""
) -> ProductRuntimePlan:
    """Discover a real project root, including a single nested application.

    A monorepo with multiple equally plausible roots is not guessed: the
    caller receives an unknown root and can ask the user which app to work on.
    """
    roots = {""}
    for path in files:
        if Path(path).name in {"package.json", "go.mod", "pyproject.toml", "requirements.txt"}:
            parent = str(Path(path).parent)
            if parent != "." and len(Path(parent).parts) <= 3:
                roots.add(parent)
    candidates = [(root, detect_project(files, root=root)) for root in sorted(roots)]
    known = [(root, profile) for root, profile in candidates if profile.kind != "unknown"]
    chosen: tuple[str, Any] | None = next(
        ((root, profile) for root, profile in known if not root), None
    )
    if chosen is None and len(known) == 1:
        chosen = known[0]
    root, profile = chosen if chosen is not None else ("", detect_project({}))
    scoped = {
        path[len(root) + 1:] if root else path: content
        for path, content in files.items()
        if not root or path.startswith(root + "/")
    }
    kind: str = profile.kind
    commands: dict[str, ProductCommand] = {}
    test_argv, build_argv, start_argv = profile.test_argv, profile.build_argv, profile.start_argv
    if kind == "unknown" and any(
        Path(path).name.startswith("test_") and path.endswith(".py") for path in scoped
    ):
        kind = "python"
        test_argv = ("python3", "-B", "-m", "unittest", "discover", "-v")
        build_argv = ("python3", "-B", "-m", "compileall", "-q", ".")
    if kind == "node":
        try:
            package = json.loads(scoped["package.json"])
        except (KeyError, TypeError, ValueError):
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        manager = (
            "pnpm" if "pnpm-lock.yaml" in scoped else "yarn" if "yarn.lock" in scoped else "npm"
        )
        declared_manager = package.get("packageManager", "") if isinstance(package, dict) else ""
        if isinstance(declared_manager, str):
            matched_manager = re.fullmatch(r"(npm|pnpm|yarn)@[0-9A-Za-z.+_-]+", declared_manager)
            if matched_manager:
                manager = matched_manager.group(1)
        test_argv = (manager, "run", "test") if "test" in scripts else ()
        build_argv = (manager, "run", "build") if "build" in scripts else ()
        start_script = "dev" if "dev" in scripts else "start" if "start" in scripts else ""
        start_argv = (manager, "run", start_script) if start_script else ()
        for command_id in ("lint", "typecheck", "check"):
            if command_id in scripts:
                commands[command_id] = ProductCommand(
                    argv=(manager, "run", command_id), cwd=root, purpose="build",
                    description=f"Run the project's {command_id} script",
                )
    if test_argv:
        if test_argv[0] == "python3" and "-B" not in test_argv:
            test_argv = ("python3", "-B", *test_argv[1:])
        commands["tests"] = ProductCommand(
            argv=test_argv, cwd=root, purpose="test", description="Run the discovered test suite"
        )
    if kind == "python":
        # compileall writes bytecode and traverses masked metadata directories.
        # Syntax checking needs neither action; compile() never executes code.
        build_argv = ("python3", "-B", "-c", (
            "import os,pathlib,sys\n"
            "failed=0;count=0\n"
            "for folder,dirs,files in os.walk('.',followlinks=False):\n"
            " dirs[:]=[d for d in dirs if d not in {'.git','.mgar','.env','.venv',"
            "'node_modules','__pycache__'} and not pathlib.Path(folder,d).is_symlink()]\n"
            " for name in sorted(files):\n"
            "  path=pathlib.Path(folder,name)\n"
            "  if path.suffix!='.py' or path.is_symlink(): continue\n"
            "  try: compile(path.read_bytes(),str(path),'exec');count+=1\n"
            "  except (OSError,SyntaxError) as error: print(error);failed+=1\n"
            "print('Syntax checked',count,'Python files');sys.exit(bool(failed))\n"
        ))
    if build_argv:
        commands["build"] = ProductCommand(
            argv=build_argv, cwd=root, purpose="build", description="Build or check project syntax"
        )
    runtime_probe = {
        "python": ("python3", "--version"),
        "node": ("node", "--version"),
        "go": ("python3", "-B", "-c", (
            "import os;os.environ['GOTOOLCHAIN']='local';os.execvp('go',['go','version'])"
        )),
    }.get(kind)
    if runtime_probe:
        commands["runtime_info"] = ProductCommand(
            argv=runtime_probe, cwd=root, description="Inspect the pinned runtime version"
        )
    readiness = None
    if kind == "node" and start_argv:
        script_text = str(scripts.get(start_argv[-1], ""))
        match = re.search(r"(?:--port[= ]|PORT=)(\d{2,5})\b", script_text)
        port = int(match.group(1)) if match else 5173 if "vite" in script_text else 3000
        readiness = ReadinessProbe(kind="http", port=port, timeout_seconds=20)
    if "index.html" in scoped and not start_argv:
        start_argv = ("python3", "-B", "-m", "http.server", "8000", "--bind", "127.0.0.1")
        readiness = ReadinessProbe(kind="http", port=8000)
        if kind == "unknown":
            kind = "static"
        commands.setdefault("runtime_info", ProductCommand(argv=("python3", "--version")))
    if not start_argv and "app.py" in scoped and "__main__" in scoped["app.py"]:
        match = re.search(r"(?:port\s*=|PORT\s*=)\s*(\d{2,5})\b", scoped["app.py"])
        if match:
            start_argv = ("python3", "-B", "app.py")
            readiness = ReadinessProbe(kind="http", port=int(match.group(1)))
    if kind in {"unknown", "static"}:
        commands["runtime_info"] = ProductCommand(argv=("python3", "--version"), cwd=root)
        # A new repository can grow into an app without first supplying a
        # manifest. This validator checks content/syntax only, never claims
        # behavioral testing, and fails when no deliverable exists.
        commands["build"] = ProductCommand(
            argv=("python3", "-B", "-c", (
                "import os,pathlib,sys,json\n"
                "from html.parser import HTMLParser\n"
                "count=failed=0\n"
                "extensions={'.py','.html','.htm','.css','.js','.jsx','.ts','.tsx',"
                "'.json','.md','.txt','.svg'}\n"
                "for folder,dirs,files in os.walk('.',followlinks=False):\n"
                " dirs[:]=[d for d in dirs if d not in {'.git','.mgar','.env','.venv',"
                "'node_modules','__pycache__','.harness-tmp'} "
                "and not pathlib.Path(folder,d).is_symlink()]\n"
                " for name in sorted(files):\n"
                "  path=pathlib.Path(folder,name)\n"
                "  if path.is_symlink() or path.suffix not in extensions "
                "or name in {'AGENTS.md','SKILL.md'}: continue\n"
                "  try:\n"
                "   with path.open('rb') as stream: data=stream.read(2000001)\n"
                "   if len(data)>2000000: raise ValueError('deliverable byte limit')\n"
                "   text=data.decode('utf-8')\n"
                "   if not text.strip() or chr(0) in text:\n"
                "    raise ValueError('empty/binary content')\n"
                "   if path.suffix=='.py': compile(data,str(path),'exec')\n"
                "   if path.suffix=='.json': json.loads(text)\n"
                "   if path.suffix in {'.html','.htm'}:\n"
                "    if '<' not in text or '>' not in text:\n"
                "     raise ValueError('HTML markup missing')\n"
                "    parser=HTMLParser();parser.feed(text);parser.close()\n"
                "   count+=1\n"
                "  except (OSError,ValueError,SyntaxError) as error: print(path,error);failed+=1\n"
                "print('Build-only verification:',count,'deliverables; behavior tests not run')\n"
                "if not count: print('No deliverable files; validation failed')\n"
                "sys.exit(bool(failed or not count))\n"
            )), cwd=root, purpose="build", description="Validate deliverable content and syntax",
        )
        commands["python_tests"] = ProductCommand(
            argv=("python3", "-B", "-m", "unittest", "discover", "-v"),
            cwd=root, purpose="test",
            description="Run real unittest discovery if tests are added during this task",
        )
    return ProductRuntimePlan(
        kind=cast(Any, kind), root=root, commands=commands, start_argv=start_argv,
        readiness=readiness, dependency_files=profile.dependency_files, image_lock=image_lock,
        verification_scope="tests" if test_argv else "build_only",
    )


def validate_product_image_lock(path: Path) -> dict[str, Any]:
    """Validate an operator-owned immutable image lock, never a model path."""
    image_id = IsolatedExecutionBroker._image_from_lock(Path(path))
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise DomainError("SANDBOX_NOT_PREPARED", "Image id must be a full SHA256", 409)
    return {"image_id": image_id, "image_lock": str(Path(path).resolve())}


def _dependency_names(plan: ProductRuntimePlan, files: Mapping[str, str]) -> list[str]:
    prefix = plan.root + "/" if plan.root else ""
    names: set[str] = set()
    if plan.kind == "python":
        lines = files.get(prefix + "requirements.txt", "").splitlines()
        try:
            metadata = tomllib.loads(files.get(prefix + "pyproject.toml", ""))
            declared = metadata.get("project", {}).get("dependencies", [])
            if isinstance(declared, list):
                lines.extend(value for value in declared if isinstance(value, str))
        except (ValueError, AttributeError):
            pass
        for line in lines:
            # Options, URLs and editable installs require their own source
            # preparation. Never turn repository text into a shell command.
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[.*?\])?(?:\s*[<>=!~; ]|$)",
                             line.strip())
            if match:
                names.add(match.group(1))
    elif plan.kind == "node":
        try:
            metadata = json.loads(files.get(prefix + "package.json", "{}"))
            for group in ("dependencies", "devDependencies"):
                values = metadata.get(group, {})
                if isinstance(values, dict):
                    names.update(name for name in values if isinstance(name, str)
                                 and re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+", name))
        except (ValueError, AttributeError):
            pass
    return sorted(names)[:512]


def _python_version_matches(specification: str, version: tuple[int, int, int]) -> bool | None:
    """Conservative numeric PEP440 subset; unsupported syntax stays unverified."""
    if not specification or len(specification) > 1000:
        return None
    for term in specification.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.(?:\d+|\*)){0,2})\s*", term)
        if match is None:
            return None
        operation, raw = match.groups()
        fields = raw.split(".")
        if "*" in fields:
            if operation not in {"==", "!="} or fields[-1] != "*" or fields.count("*") != 1:
                return None
            prefix = tuple(int(field) for field in fields[:-1])
            equal = version[:len(prefix)] == prefix
            passed = equal if operation == "==" else not equal
        else:
            numbers = tuple(int(field) for field in fields)
            bound = (*numbers, *(0 for _ in range(3 - len(numbers))))
            if operation == "~=":
                if len(numbers) < 2:
                    return None
                upper = (numbers[0] + 1, 0, 0) if len(numbers) == 2 else (
                    numbers[0], numbers[1] + 1, 0
                )
                passed = bound <= version < upper
            else:
                passed = {
                    ">=": version >= bound, "<=": version <= bound,
                    ">": version > bound, "<": version < bound,
                    "==": version == bound, "!=": version != bound,
                }[operation]
        if not passed:
            return False
    return True


def product_readiness(path: Path, image_lock: Path) -> dict[str, Any]:
    """Zero-model preflight, with actual isolated runtime evidence.

    It never runs package install scripts or project tests. Source dependencies
    require a prepared image; a missing dependency is an environment state.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise DomainError("WORKSPACE_PATH_DENIED", "Project root is unavailable", 403)
    inventory = _scan(path)[0]
    files = {}
    names = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.mod",
             "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini",
             "index.html", "app.py"}
    for relative in inventory:
        if Path(relative).name in names or Path(relative).name.startswith("test_"):
            try:
                files[relative] = _read_regular_bytes(path, relative, 2_000_000).decode(
                    "utf-8", "replace"
                )
            except DomainError:
                continue
    plan = discover_product_plan(files, image_lock=str(image_lock))
    blockers: list[dict[str, str]] = []
    observations: list[dict[str, Any]] = []
    dependency_names = _dependency_names(plan, files)
    dependency_status = "not_declared" if not dependency_names else "unchecked"
    try:
        validate_product_image_lock(image_lock)
        probe = plan.commands.get("runtime_info")
        if probe is not None:
            lease = issue_lease(
                mode="investigate", actor="product-preflight", workspace_id="product-preflight",
                repository="local/preflight", execution_epoch=1, read_paths=("",),
                allowed_commands=tuple(sorted(set((*plan.programs, "python3")))),
                timeout_seconds=15, max_calls=6,
            )
            grant = CapabilityGrant(
                actor=lease.actor, workspace_id=lease.workspace_id, repository=lease.repository,
                read_paths=lease.read_paths, execute=True, allowed_commands=lease.allowed_commands,
                max_seconds=15,
            )
            broker = IsolatedExecutionBroker(Path(path), lease, lock_path=image_lock)
            receipt = broker.execute(
                ExecutionRequest(
                    operation_id="preflight-" + uuid4().hex[:12], purpose="command",
                    argv=probe.argv, cwd=probe.cwd, timeout_seconds=15, execution_epoch=1,
                ), grant, CancellationToken(),
            )
            observations.append({
                "check": "runtime", "exit_code": receipt.exit_code,
                "output": (receipt.stdout + receipt.stderr)[-1000:],
            })
            if receipt.exit_code:
                blockers.append({
                    "code": "PRODUCT_RUNTIME_MISSING",
                    "detail": f"The pinned image cannot run {probe.argv[0]}",
                    "action": "Select a prepared image lock containing the required runtime.",
                })
            prefix = plan.root + "/" if plan.root else ""
            if plan.kind == "python" and not receipt.exit_code:
                pyproject = files.get(prefix + "pyproject.toml", "")
                specification: Any = None
                try:
                    specification = tomllib.loads(pyproject).get("project", {}).get(
                        "requires-python"
                    )
                except (ValueError, AttributeError):
                    specification = "invalid project metadata"
                if specification is not None:
                    found = re.search(r"Python (\d+)\.(\d+)\.(\d+)",
                                      receipt.stdout + receipt.stderr)
                    actual = tuple(int(part) for part in found.groups()) if found else None
                    matched = _python_version_matches(specification, cast(Any, actual)) if (
                        isinstance(specification, str) and actual is not None
                    ) else None
                    observations.append({
                        "check": "python_requires", "requirement": str(specification)[:1000],
                        "actual": list(actual) if actual is not None else None,
                        "status": "satisfied" if matched is True else
                        "not_satisfied" if matched is False else "not_verified",
                    })
                    if matched is not True:
                        blockers.append({
                            "code": "PRODUCT_PYTHON_VERSION_REQUIRED",
                            "detail": "Pinned Python does not satisfy a verified requires-python.",
                            "action": "Provide a matching pinned Python image or supported bounds.",
                        })
            if plan.kind == "node" and not receipt.exit_code:
                package = json.loads(files.get(prefix + "package.json", "{}"))
                manager = next((command.argv[0] for command in plan.commands.values()
                                if command.argv[0] in {"npm", "pnpm", "yarn"}), None)
                if manager is not None:
                    check = broker.execute(ExecutionRequest(
                        operation_id="preflight-" + uuid4().hex[:12], purpose="command",
                        argv=("python3", "-B", "-c", (
                            "import os,sys\n"
                            "env={**os.environ,'COREPACK_ENABLE_NETWORK':'0',"
                            "'COREPACK_ENABLE_DOWNLOAD_PROMPT':'0','npm_config_offline':'true'}\n"
                            "os.execvpe(sys.argv[1],[sys.argv[1],'--version'],env)\n"
                        ), manager), cwd=plan.root, timeout_seconds=15,
                        execution_epoch=1,
                    ), grant, CancellationToken())
                    observed_version = (check.stdout + check.stderr).strip()
                    declared = package.get("packageManager", "") if isinstance(
                        package, dict
                    ) else ""
                    expected = declared.split("@", 1)[1].split("+", 1)[0] if isinstance(
                        declared, str
                    ) and declared.startswith(manager + "@") else ""
                    matched = not check.exit_code and (not expected or observed_version == expected)
                    observations.append({
                        "check": "package_manager", "program": manager,
                        "exit_code": check.exit_code, "output": observed_version[-1000:],
                        "expected": expected, "status": "ready" if matched else "not_verified",
                    })
                    if not matched:
                        blockers.append({
                            "code": "PRODUCT_PACKAGE_MANAGER_REQUIRED",
                            "detail": f"Pinned image cannot verify selected {manager} version.",
                            "action": "Select an approved image with the declared package manager.",
                        })
            if plan.kind == "go" and not receipt.exit_code:
                check = broker.execute(ExecutionRequest(
                    operation_id="preflight-" + uuid4().hex[:12], purpose="command",
                    argv=("python3", "-B", "-c", (
                        "import json,os,subprocess,sys,pathlib\n"
                        "env={**os.environ,'GOTOOLCHAIN':'local','GOPROXY':'off',"
                        "'GOSUMDB':'off','GOVCS':'*:off'}\n"
                        "result=subprocess.run(['go','list','-m','-mod=readonly','-json','all'],"
                        "env=env,capture_output=True,timeout=8)\n"
                        "if result.returncode:\n"
                        " print((result.stdout+result.stderr).decode('utf-8','replace')[-4000:]);"
                        "sys.exit(1)\n"
                        "if len(result.stdout)>1048576:\n"
                        " raise ValueError('module graph too large')\n"
                        "text=result.stdout.decode();decoder=json.JSONDecoder();modules=[]\n"
                        "while text.strip():\n"
                        " item,index=decoder.raw_decode(text.lstrip());text=text.lstrip()[index:]\n"
                        " modules.append(item)\n"
                        " if len(modules)>1024: raise ValueError('module limit')\n"
                        "missing=[m.get('Path','unknown') for m in modules if not m.get('Main') "
                        "and (not m.get('Dir') or not pathlib.Path(m['Dir']).is_dir())]\n"
                        "print(json.dumps({'modules':len(modules),'missing_sources':missing[:100],"
                        "'toolchain':'local','proxy':'off'}));sys.exit(bool(missing))\n"
                    )), cwd=plan.root, timeout_seconds=12, execution_epoch=1,
                ), grant, CancellationToken())
                observations.append({"check": "go_modules", "exit_code": check.exit_code,
                                     "output": (check.stdout + check.stderr)[-4000:],
                                     "network": "none", "toolchain_download": "disabled"})
                dependency_status = "available" if not check.exit_code else "not_verified"
                if check.exit_code:
                    blockers.append({
                        "code": "PRODUCT_GO_ENVIRONMENT_REQUIRED",
                        "detail": "Pinned Go toolchain/module sources are unavailable offline.",
                        "action": "Select an image with matching Go and the locked module cache.",
                    })
            if plan.kind == "python" and "pytest" in plan.test_argv:
                check = broker.execute(
                    ExecutionRequest(
                        operation_id="preflight-" + uuid4().hex[:12], purpose="command",
                        argv=("python3", "-c", "import pytest;print(pytest.__version__)"),
                        cwd=plan.root, timeout_seconds=15, execution_epoch=1,
                    ), grant, CancellationToken(),
                )
                observations.append({"check": "pytest", "exit_code": check.exit_code})
                if check.exit_code:
                    blockers.append({
                        "code": "PRODUCT_TEST_RUNNER_MISSING", "detail": "pytest is unavailable",
                        "action": "Select an approved image containing pytest.",
                    })
            if dependency_names and not blockers and plan.kind in {"python", "node"}:
                if plan.kind == "python":
                    argv = ("python3", "-c", (
                        "import importlib.metadata,json,sys\n"
                        "missing=[]\n"
                        "for name in json.loads(sys.argv[1]):\n"
                        " try: importlib.metadata.distribution(name)\n"
                        " except importlib.metadata.PackageNotFoundError: missing.append(name)\n"
                        "print(json.dumps(missing))\n"
                    ), json.dumps(dependency_names))
                else:
                    argv = ("node", "-e", (
                        "const names=JSON.parse(process.argv[1]);"
                        "console.log(JSON.stringify(names.filter(name=>{try{"
                        "require.resolve(name,{paths:[process.cwd()]});return false"
                        "}catch{return true}})))"
                    ), json.dumps(dependency_names))
                check = broker.execute(
                    ExecutionRequest(
                        operation_id="preflight-" + uuid4().hex[:12], purpose="command",
                        argv=argv, cwd=plan.root, timeout_seconds=15, execution_epoch=1,
                    ), grant, CancellationToken(),
                )
                try:
                    missing = json.loads(check.stdout.strip()) if not check.exit_code else []
                    if not isinstance(missing, list):
                        raise ValueError("dependency probe output")
                    dependency_status = "missing" if missing else "available"
                except ValueError:
                    missing, dependency_status = [], "unchecked"
                observations.append({"check": "dependencies", "exit_code": check.exit_code,
                                     "missing": missing, "versions": "not_verified"})
                if missing or check.exit_code or dependency_status == "unchecked":
                    blockers.append({
                        "code": "PRODUCT_DEPENDENCIES_MISSING",
                        "detail": "Prepared image dependencies unavailable: "
                        + ", ".join(str(name) for name in missing[:20]),
                        "action": "Select an approved image with locked project dependencies.",
                    })
    except DomainError as exc:
        blockers.append({"code": exc.code, "detail": str(exc),
                         "action": "Prepare Docker and select a valid pinned sandbox image."})
    return {
        "status": "waiting_environment" if blockers else "ready",
        "plan": plan.model_dump(mode="json"), "blockers": blockers,
        "observations": observations,
        "dependencies": {
            "status": dependency_status,
            "files": list(plan.dependency_files),
            "action": "Use an approved image with locked dependencies; no network install.",
        },
        "preview_available": bool(plan.start_argv and plan.readiness),
        "verification_scope": plan.verification_scope,
    }


class ProductCodingToolRegistry(CodingToolRegistry):
    """Expose frozen project commands through the same lease and IO journal."""

    def __init__(self, *args: Any, runtime_plan: ProductRuntimePlan, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.runtime_plan = runtime_plan

    def specs(self) -> tuple[ToolSpec, ...]:
        command_ids = sorted(self.runtime_plan.commands)
        base = tuple(spec for spec in super().specs()
                     if spec.name != "run_tests" or self.runtime_plan.test_argv)
        return (*base, ToolSpec(
            name="run_command", capability="execute",
            description=(
                "Run a server-approved project command in the isolated workspace. "
                "Choose an exact command_id; no shell strings or arbitrary argv. "
                "For a missing capability use ask_user to describe the concrete need. "
                "The exact argv and cwd are already bound by the server. Available commands: "
                + ", ".join(command_ids)[:600]
            ),
            input_schema={
                "type": "object", "properties": {
                    "command_id": {"type": "string", "enum": command_ids}
                },
                "required": ["command_id"], "additionalProperties": False,
            },
        ))

    def resolve_command(self, call: HarnessToolCall) -> tuple[str, ...] | None:
        if call.name != "run_command":
            return super().resolve_command(call)
        command = self._command(call)
        return command.argv if command is not None else None

    def _command(self, call: HarnessToolCall) -> ProductCommand | None:
        if set(call.arguments) != {"command_id"}:
            return None
        command_id = call.arguments["command_id"]
        if not isinstance(command_id, str):
            return None
        return self.runtime_plan.commands.get(command_id)

    def invoke(
        self, call: HarnessToolCall, context: ToolContext, cancellation: CancellationToken
    ) -> HarnessToolResult:
        if call.name == "run_tests":
            command = self.runtime_plan.commands.get("tests")
            if command is not None and super().resolve_command(call) is not None:
                return self.invoke(
                    call.model_copy(update={
                        "name": "run_command", "arguments": {"command_id": "tests"}
                    }), context, cancellation,
                )
        if call.name != "run_command":
            return super().invoke(call, context, cancellation)
        if self.recovery_io is not None:
            return self.recovery_io.invoke(
                context.operation_id, "tool", call.model_dump(mode="json"),
                lambda: self._run_product_command(call, context, cancellation),
                encode=lambda result: result.model_dump(mode="json"),
                decode=HarnessToolResult.model_validate, read_only=False,
            )
        return self._run_product_command(call, context, cancellation)

    def _run_product_command(
        self, call: HarnessToolCall, context: ToolContext, cancellation: CancellationToken
    ) -> HarnessToolResult:
        command = self._command(call)
        if command is None:
            return HarnessToolResult(
                call_id=call.call_id, status="error", code="PRODUCT_COMMAND_UNKNOWN",
                content=("No command was executed. Choose a command_id from "
                         + json.dumps(sorted(self.runtime_plan.commands))
                         + ". Use ask_user for an additional capability."),
                data={"correctable": True, "executed": False,
                      "allowed_command_ids": sorted(self.runtime_plan.commands)},
            )
        return self._run_command(call, command, context, cancellation)

    def _run_command(
        self, call: HarnessToolCall, command: ProductCommand, context: ToolContext,
        cancellation: CancellationToken,
    ) -> HarnessToolResult:
        try:
            cancellation.ensure_active()
            request = ExecutionRequest(
                operation_id=context.operation_id, purpose=command.purpose, argv=command.argv,
                cwd=command.cwd, timeout_seconds=self.command_timeout_seconds,
                execution_epoch=context.task.execution_epoch,
            )
            started = time.monotonic()
            handle = self.session.run(
                request, context.grant, cancellation, purpose=command.purpose
            )
            self._active_commands.add(handle.handle_id)
            try:
                final = self.session.wait(
                    handle.handle_id, timeout=self.command_timeout_seconds + 5
                )
                provenance = self.session.assert_authority(handle.handle_id)
                page = self.session.read_log(handle.handle_id)
                chunks = [page.data]
                while not page.eof:
                    next_page = self.session.read_log(handle.handle_id, offset=page.next_offset)
                    if next_page.next_offset <= page.next_offset:
                        raise DomainError("HARNESS_LOG_INCOMPLETE", "Log did not advance", 409)
                    page = next_page
                    chunks.append(page.data)
                output = "".join(chunks)
            finally:
                self._active_commands.discard(handle.handle_id)
            complete = not (page.truncated or page.dropped_bytes)
            observed = context.model_copy(update={
                "workspace_tree": self.workspace.snapshot().tree_hash
            })
            reference = self.context_tools.archive_command_log(
                output, observed, provenance=provenance, complete=complete,
                total_bytes=page.total_bytes,
            )
            run, failed = 0, 0
            status = _execution_status(final.state, final.exit_code)
            if command.purpose == "test":
                from mgar_v3.harness.service import _test_status

                parsed, run, failed, _, _ = _test_status(
                    self.kind, output, final.exit_code, argv=command.argv
                )
                if parsed != "passed":
                    status = "failed" if parsed == "failed" else "error"
            if not complete:
                status = "error"
            receipt = ExecutionReceipt(
                receipt_id=canonical_hash({"handle": handle.handle_id, "tree": final.tree_hash,
                                           "log": sha256_text(output),
                                           "command": command.model_dump(mode="json")}),
                operation_id=context.operation_id, purpose=command.purpose,
                status=cast(Any, status),
                argv=command.argv, cwd=command.cwd,
                exit_code=final.exit_code if final.exit_code is not None else -1,
                stdout=output, stderr="", duration_seconds=time.monotonic() - started,
                tree_hash=final.tree_hash, log_sha256=sha256_text(output),
                environment_hash=final.environment_hash, tests_run=run, tests_failed=failed,
                provenance=provenance,
            )
            self.session.assert_authority(handle.handle_id)
            return HarnessToolResult(
                call_id=call.call_id, status="ok" if status == "passed" else "error",
                content=_test_feedback(f"Command {call.arguments['command_id']}: {status}", output)
                + artifact_trailer(reference),
                data={"receipt": receipt.model_dump(mode="json"), "artifact": reference,
                      "truncated": not complete, "command_id": call.arguments["command_id"]},
            )
        except DomainError as exc:
            return HarnessToolResult(
                call_id=call.call_id, status="denied" if exc.status == 403 else "error",
                code=exc.code, content=str(exc),
            )


class ProductPreview:
    """An owned, bounded preview; the HTTP API supplies the authenticated proxy."""

    def __init__(
        self, session: CommandSession, grant: CapabilityGrant, cancellation: CancellationToken,
        plan: ProductRuntimePlan,
    ) -> None:
        self.session, self.grant, self.cancellation, self.plan = session, grant, cancellation, plan
        self.launch: ServiceLaunch | None = None

    def start(self) -> dict[str, Any]:
        if not self.plan.start_argv or self.plan.readiness is None:
            raise DomainError(
                "PRODUCT_START_PLAN_REQUIRED", "No project start command and readiness plan", 409
            )
        if self.launch is None:
            self.launch = self.session.start_service(
                ExecutionRequest(
                    operation_id="preview-" + uuid4().hex[:12], purpose="command",
                    argv=self.plan.start_argv, cwd=self.plan.root,
                    timeout_seconds=min(600, self.grant.max_seconds),
                    execution_epoch=self.session.broker.authority.peek().execution_epoch,
                ), self.grant, self.cancellation, self.plan.readiness,
            )
        handle = self.session.poll(self.launch.handle.handle_id)
        return {"status": handle.state, "handle_id": handle.handle_id,
                "port": self.launch.container_port, "host_reachable": False}

    def fetch(self, path: str = "/") -> tuple[int, str, bytes]:
        if self.launch is None:
            raise DomainError("PRODUCT_PREVIEW_NOT_STARTED", "Start preview first", 409)
        return self.session.fetch_http(
            self.launch.handle.handle_id, port=self.launch.container_port, path=path
        )

    def close(self) -> None:
        self.cancellation.cancel()
        self.session.close()
