"""Freeze independent reporters from declared public commands and package metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from mgar_v3.domain import digest
from mgar_v3.harness.project_commands import infer_test_runner
from mgar_v3.harness.regression_types import RegressionCommand, RegressionPlan, TestFramework


def regression_plan(
    commands: Sequence[Sequence[str]],
    *,
    package: Mapping[str, Any] | None = None,
) -> RegressionPlan:
    suites = []
    for public in commands:
        original = tuple(public)
        if not original:
            continue
        argv = list(original)
        reporter = infer_test_runner(argv)
        if original[0] == "node" and "--test" in original:
            reporter = "node"
        if reporter == "pytest":
            argv += ["--verbosity=2", "--maxfail=0"]
            framework = "pytest"
        elif reporter == "unittest":
            argv = [v for v in argv if v not in {"-f", "--failfast"}]
            if "-v" not in argv and "--verbose" not in argv:
                argv += ["-v"]
            framework = "unittest"
        elif reporter == "go":
            argv += ["-json"]
            if not any(v == "-count" or v.startswith("-count=") for v in argv):
                argv += ["-count=1"]
            framework = "go-json"
        else:
            script = ""
            if original[:2] == ("npm", "test"):
                script = str((package or {}).get("scripts", {}).get("test", ""))
            elif original[:2] == ("npm", "run") and len(original) >= 3:
                script = str((package or {}).get("scripts", {}).get(original[2], ""))
            if re.search(r"\bmocha\b", script):
                reporter = "mocha"
            elif re.search(r"\bava\b", script):
                reporter = "ava"
            elif re.search(r"\bnode\s+--test\b", script):
                reporter = "node"
            if reporter == "mocha":
                extra = ["--reporter", "json", "--no-bail"]
                framework = "mocha-json"
            elif reporter == "ava":
                extra = ["--tap", "--no-fail-fast", "--no-cache"]
                framework = "tap"
            elif original[0].rsplit("/", 1)[-1] == "node" and "--test" in original:
                extra = ["--test-reporter=tap"]
                framework = "tap"
            elif script and reporter == "node":
                extra = ["--test-reporter=tap"]
                framework = "tap"
            else:
                extra, framework = [], "unconfigured"
            if extra:
                if original[0] in {"npm", "pnpm"} and "--" not in argv:
                    argv.append("--")
                # Node options must precede positional test paths.
                if original[0] == "node" and reporter == "node":
                    argv[1:1] = extra
                else:
                    argv += extra
        suites.append(
            RegressionCommand(
                suite_id="public-" + digest({"argv": original, "cwd": "."})[:20],
                public_argv=original,
                argv=tuple(argv),
                framework=cast(TestFramework, framework),
            )
        )
    scripts = (package or {}).get("scripts", {})
    return RegressionPlan(commands=tuple(suites), package_scripts=dict(scripts))
