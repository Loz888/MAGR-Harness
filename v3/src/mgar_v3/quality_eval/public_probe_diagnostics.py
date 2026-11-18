"""Prepare a new public probe revision with bounded observed failure details.

This never rewrites a frozen asset or changes a check's assertions/commands.
The standalone helper is embedded because sandbox projects cannot import the
host Harness package. Only public subprocess output, never provider data, is
available to this helper.
"""

from __future__ import annotations

import ast

from mgar_v3.domain import DomainError

HELPER_NAME = "_h12_public_failure_detail"
DIAGNOSTICS_NAME = "_h12_public_command_diagnostics"
HELPER = '''_h12_public_command_diagnostics = []
def _h12_public_failure_detail(error):
    from subprocess import CompletedProcess
    import base64
    message = str(error)
    if message:
        return message[:800]
    trace = error.__traceback__
    observed = None
    line = 0
    for _ in range(8):
        if trace is None:
            break
        line = trace.tb_lineno
        processes = [value for value in list(trace.tb_frame.f_locals.values())[:128]
                     if isinstance(value, CompletedProcess)]
        if processes:
            observed = processes[-1]
        trace = trace.tb_next
    if observed is None:
        return "Assertion failed without a message at line " + str(line)
    for label, value in (("stderr", observed.stderr), ("stdout", observed.stdout)):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(raw, bytes) or not raw:
            continue
        retained = raw[:65536]
        _h12_public_command_diagnostics.append({
            "stream": label, "total_bytes": len(raw), "retained_bytes": len(retained),
            "truncated": len(retained) < len(raw),
            "retained_base64": base64.b64encode(retained).decode("ascii"),
        })
    def text(value, limit):
        if isinstance(value, bytes):
            return value[:limit].decode("utf-8", "replace")
        return value[:limit] if isinstance(value, str) else ""
    code = observed.returncode if isinstance(observed.returncode, int) else "unknown"
    return ("Observed subprocess exit=" + str(code)
            + "; stderr=" + text(observed.stderr, 600)
            + "; stdout=" + text(observed.stdout, 160))[:800]
'''


def instrument_public_probe(source: str) -> str:
    """Change only reporter details and optional bounded diagnostic fields.

    Unknown reporters are refused. The caller must freeze the new byte hash;
    evaluating it under an old plan is still forbidden by read_asset().
    """
    if len(source.encode()) > 200000:
        raise DomainError("PUBLIC_PROBE_DIAGNOSTICS_UNSUPPORTED", "Public probe is too large", 422)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise DomainError(
            "PUBLIC_PROBE_DIAGNOSTICS_UNSUPPORTED", "Public probe is not Python", 422
        ) from exc
    if any((isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == HELPER_NAME)
           or isinstance(node, ast.Name) and node.id == DIAGNOSTICS_NAME
           for node in ast.walk(tree)):
        raise DomainError(
            "PUBLIC_PROBE_DIAGNOSTICS_UNSUPPORTED", "Probe already has this diagnostic helper", 422
        )
    replacements = 0
    emitters = 0
    for main in tree.body:
        if not isinstance(main, ast.FunctionDef) or main.name != "main":
            continue
        for handler in ast.walk(main):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name
                    and isinstance(handler.type, ast.Name) and handler.type.id == "AssertionError"):
                continue
            expected = ast.dump(ast.parse(f"str({handler.name})[:800]", mode="eval").body)
            for statement in handler.body:
                if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)
                        and statement.targets[0].id == "detail"
                        and ast.dump(statement.value) == expected):
                    statement.value = ast.Call(
                        func=ast.Name(id=HELPER_NAME, ctx=ast.Load()),
                        args=[ast.Name(id=handler.name, ctx=ast.Load())], keywords=[],
                    )
                    replacements += 1
        for node in ast.walk(main):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "json"
                    and node.func.attr == "dumps" and node.args
                    and isinstance(node.args[0], ast.Dict)):
                frame = node.args[0]
                frame.keys.append(None)
                frame.values.append(ast.IfExp(
                    test=ast.Name(id=DIAGNOSTICS_NAME, ctx=ast.Load()),
                    body=ast.Dict(keys=[ast.Constant("diagnostics")],
                                  values=[ast.Name(id=DIAGNOSTICS_NAME, ctx=ast.Load())]),
                    orelse=ast.Dict(keys=[], values=[]),
                ))
                emitters += 1
    if replacements != 1 or emitters != 1:
        raise DomainError(
            "PUBLIC_PROBE_DIAGNOSTICS_UNSUPPORTED", "One explicit public reporter required", 422
        )
    # Preserve module docstrings and future imports at the start of the file.
    insertion = 0
    for node in tree.body:
        if ((isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
             and isinstance(node.value.value, str))
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insertion += 1
        else:
            break
    tree.body[insertion:insertion] = ast.parse(HELPER).body
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
