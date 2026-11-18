"""Explicit dev-only permission corrections bound to original public source."""

from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path
from typing import Any

from mgar_v3.harness.contracts import canonical_hash, normalize_relative_path
from mgar_v3.quality_eval.harness_v4 import CatalogError


def load_scope_amendment(
    root: Path, reference: dict[str, str], tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    from mgar_v3.quality_eval.h12_v2_freeze import _public_asset, _public_document

    path = _public_asset(root, reference)
    document = _public_document(root, path, reference['sha256'])
    changes = document.get('changes')
    if (document.get('schema') != 'h12-development-scope-amendment-v1'
            or document.get('status') != 'REVIEWED'
            or document.get('base_tasks_sha256') != canonical_hash(tasks)
            or not isinstance(changes, list) or not 1 <= len(changes) <= 12):
        raise CatalogError('Scope amendment is not bound to its original development tasks')
    result = copy.deepcopy(tasks)
    originals = {task['task_id']: task for task in tasks}
    bindings: dict[str, dict[str, Any]] = {}
    for change in changes:
        original = originals.get(change.get('task_id'))
        target = change.get('implementation_path')
        symbol = change.get('symbol')
        if (original is None or original['split'] != 'dev' or original['language'] != 'python'
                or original['task_id'] in bindings
                or change.get('original_task_sha256') != canonical_hash(original)
                or change.get('original_scope') != original['scope']
                or change.get('reason') != 'PUBLIC_IMPLEMENTATION_OUTSIDE_FROZEN_SCOPE'
                or not isinstance(target, str) or not target.endswith('.py')
                or normalize_relative_path(target) != target
                or any(part.startswith('.') or part in {'test', 'tests', 'verification'}
                       for part in Path(target).parts)
                or change.get('replacement_scope') != [target]
                or any(target == p or target.startswith(p.rstrip('/') + '/') or not p
                       for p in original['scope'])
                or not isinstance(symbol, str) or not symbol.isidentifier()
                or symbol not in original['goal']):
            raise CatalogError('Scope correction is not a single public implementation file')
        source = _public_asset(root, change.get('public_source'))
        try:
            module = ast.parse(source.read_bytes())
        except (SyntaxError, ValueError) as exc:
            raise CatalogError('Scope basis is not valid public Python source') from exc
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == symbol for node in module.body):
            raise CatalogError('Scope basis does not define the public objective symbol')
        bindings[original['task_id']] = {
            'path': target, 'sha256': change['public_source']['sha256'], 'symbol': symbol,
        }
        next(task for task in result if task['task_id'] == original['task_id'])['scope'] = [target]
    return {'tasks': result, 'bindings': bindings, 'document': document}


def validate_scope_basis(target: Path, binding: dict[str, Any] | None) -> None:
    """Check the basis again against the physically materialized original commit."""
    if binding is None:
        return
    path = target / binding['path']
    if (not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to(target.resolve())
            or any(p.is_symlink() for p in path.parents if p.is_relative_to(target))
            or hashlib.sha256(path.read_bytes()).hexdigest() != binding['sha256']):
        raise CatalogError('Scope amendment basis differs from the original repository tree')
