"""Accept only declared, executed public defect-test failures on original trees.

This is not the target behavior oracle: the separately frozen public probe must
still prove assertion red, and final regression requirements remain all green.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

_FAILED = re.compile(r"(?m)^FAILED\s+([^\s:]+)::[^\n]+$")
_INFRA = re.compile(
    r"(?im)^ERROR(?:\s|$)|ERROR collecting|INTERNALERROR|ImportError|ModuleNotFoundError|"
    r"KeyboardInterrupt|MemoryError|OOMKilled|No space left on device|Permission denied|"
    r"^\s*Killed\s*$|fatal Python error|MODULE_NOT_FOUND|ERR_MODULE_NOT_FOUND|SyntaxError:"
)


def public_test_argv(task: Mapping[str, Any], argv: Sequence[str]) -> tuple[str, ...]:
    """Use Node's real TAP reporter for an immutable standalone public test file.

    The Node runner executes the original file unchanged as one test. Arbitrary
    project scripts and unregistered commands do not opt into this protocol.
    """
    if (task.get('language') in {'javascript', 'typescript'} and len(argv) == 2
            and argv[0] == 'node' and argv[1].endswith(('.js', '.cjs', '.mjs'))
            and any(a.get('target') == argv[1] for a in task.get('public_assets', ()))):
        return ('node', '--test', argv[1])
    return tuple(argv)


def missing_public_go_target(output: str, allowed_paths: Sequence[str]) -> bool:
    """A feature's frozen test may reference a not-yet-implemented Go symbol.

    This proves an executed compiler-stage baseline, not an executed test or a
    passing regression. Dependency, syntax and non-public-file failures reject.
    """
    if _INFRA.search(output) or '[setup failed]' in output:
        return False
    errors = re.findall(r'(?m)^(?:\./)?([^\s:]+\.go):\d+:\d+: (.*)$', output)
    return bool(errors) and all(
        path in allowed_paths and (
            re.fullmatch(r'undefined: [\w.]+', message) is not None
            or re.fullmatch(r'[\w.]+ undefined \(type .+ has no field or method \w+\)', message)
            is not None
        ) for path, message in errors
    )


def _missing_export(output: str, source_scopes: Sequence[str]) -> bool:
    errors = re.findall(r'(?m)^E\s+ImportError: ([^\n]+)$', output)
    summaries = re.findall(r'(?m)^FAILED[^\n]+ - ImportError:', output)
    if not source_scopes or not errors or len(errors) != len(summaries):
        return False
    for error in errors:
        match = re.fullmatch(r"cannot import name .+ from .+ \(/workspace/([^\n)]+)\)", error)
        if match is None:
            return False
        path = match[1]
        if '..' in PurePosixPath(path).parts or not any(
            not scope or path == scope or path.startswith(scope + '/') for scope in source_scopes
        ):
            return False
    return True


def declared_assertion_failure(
    output: str, failed: int, allowed_public_paths: Sequence[str],
    *, language: str = 'python', missing_export_scopes: Sequence[str] = (),
) -> bool:
    """Require exact frozen pytest defect paths; reject collection/import failures.

    A test-stage exception raised by the target API may be the declared defect.
    It cannot substitute for the separate probe's strict target assertion red.
    """
    allowed = set(allowed_public_paths)
    if missing_export_scopes and _missing_export(output, missing_export_scopes):
        output = output.replace('ImportError', 'DeclaredMissingExport')
    if failed <= 0 or not allowed or _INFRA.search(output):
        return False
    if language == 'go':
        failures = re.findall(r'(?m)^--- FAIL: ([^\s]+)', output)
        if len(failures) != failed:
            return False
        for name in failures:
            block = re.search(r'(?ms)^=== RUN\s+' + re.escape(name) + r'\s*\n(.*?)^--- FAIL: '
                              + re.escape(name) + r'\s', output)
            if block is None:
                return False
            paths = re.findall(r'(?m)^\s+([^\s:]+_test\.go):\d+:', block[1])
            if not paths or not set(paths) <= allowed:
                return False
        return True
    if language in {'javascript', 'typescript', 'node'}:
        paths = re.findall(r'(?m)^not ok \d+ - /workspace/([^\r\n]+)', output)
        return len(paths) == failed and set(paths) <= allowed
    if language != 'python':
        return False
    paths = _FAILED.findall(output)
    return len(paths) == failed and set(paths) <= allowed
