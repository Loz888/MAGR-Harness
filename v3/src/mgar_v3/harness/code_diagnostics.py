"""Bounded, advisory Python diagnostics. Never import or execute project code."""

from __future__ import annotations

import ast
import builtins
import symtable
from collections.abc import Iterable
from typing import Any


def edit_diagnostics(files: dict[str, str], paths: Iterable[str]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        source = files.get(path)
        if not path.endswith('.py') or source is None or len(source.encode()) > 200_000:
            continue
        try:
            tree = ast.parse(source, filename=path)
            table = symtable.symtable(source, path, 'exec')
        except SyntaxError as error:
            diagnostics.append({'path': path, 'line': error.lineno, 'code': 'syntax_error',
                                'message': str(error.msg)[:300]})
            continue
        except (ValueError, RecursionError):
            continue
        # A star import can define names that static inspection cannot know.
        if any(isinstance(node, ast.ImportFrom) and any(a.name == '*' for a in node.names)
               for node in ast.walk(tree)):
            continue
        defined = {s.get_name() for s in table.get_symbols()
                   if s.is_assigned() or s.is_imported() or s.is_namespace()}
        defined.update(vars(builtins))
        defined.update({'__file__', '__name__', '__package__', '__spec__', '__doc__',
                        '__builtins__', '__loader__', '__annotations__', '__path__'})
        missing: set[str] = set()
        pending = [table]
        while pending:
            scope = pending.pop()
            missing.update(s.get_name() for s in scope.get_symbols()
                           if s.is_referenced() and s.is_global()
                           and s.get_name() not in defined)
            pending.extend(scope.get_children())
        # Dynamic module globals make even advisory name resolution misleading.
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id in {'exec', 'globals'} for node in ast.walk(tree)):
            continue
        positions: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in missing:
                positions[node.id] = min(positions.get(node.id, node.lineno), node.lineno)
        for name, line in sorted(positions.items(), key=lambda item: item[1]):
            diagnostics.append({
                'path': path, 'line': line, 'code': 'possibly_undefined_name', 'name': name,
                'message': f'{name!r} has no static module definition/import. '
                           'Check its definition or import before running tests.',
            })
    return diagnostics[:20]
