"""Frozen public behavior check derived only from the development objective.

Imports/setup are classified separately from target assertions. This is public
engineering feedback; independent post-seal scoring remains separate.
"""

import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT))
PLAN_ID = "h12-public-click-help-name-v1"
CASE_IDS = ("argument-retains-value", "automatic-help-option", "help-after-positional")


def load_target():
    import click
    from click.testing import CliRunner

    if not Path(click.__file__).resolve().is_relative_to(ROOT.resolve()):
        raise ImportError("click did not load from the current workspace")
    targets = {}
    for case_id in CASE_IDS:
        observed = []

        def callback(help, observed=observed):
            observed.append(help)
            click.echo("VALUE=" + help)

        command = click.command()(click.argument("help")(callback))
        targets[case_id] = (command, observed, CliRunner())
    return targets


def check_case(case_id, target):
    command, observed, runner = target[case_id]
    if case_id == "argument-retains-value":
        result = runner.invoke(command, ["payload-42"])
        assert result.exit_code == 0, "declared argument must parse successfully"
        assert observed == ["payload-42"], "automatic help must not replace the help argument"
        assert result.output.strip() == "VALUE=payload-42", "argument value must reach callback"
    elif case_id == "automatic-help-option":
        result = runner.invoke(command, ["--help"])
        assert result.exit_code == 0, "automatic --help must exit successfully"
        assert "Usage:" in result.output and "--help" in result.output, "help text must render"
        assert observed == [], "--help must not execute the command callback"
    elif case_id == "help-after-positional":
        result = runner.invoke(command, ["payload-42", "--help"])
        assert result.exit_code == 0 and "Usage:" in result.output, "--help must still be eager"
        assert observed == [], "eager --help must not execute the callback"


def main():
    setup_error = None
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            target = load_target()
        if captured.getvalue():
            setup_error = "unexpected output during import"
    except BaseException as error:
        target = None
        setup_error = "import/setup: " + type(error).__name__
    results = []
    for case_id in CASE_IDS:
        if setup_error is not None:
            results.append({"id": case_id, "status": "error", "detail": setup_error})
            continue
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                check_case(case_id, target)
            if captured.getvalue():
                raise RuntimeError("unexpected output outside the target result")
            result = {"id": case_id, "status": "passed"}
        except AssertionError as error:
            result = {"id": case_id, "status": "assertion_failed", "detail": str(error)[:500]}
        except BaseException as error:
            result = {
                "id": case_id,
                "status": "error",
                "detail": "execution: " + type(error).__name__,
            }
        results.append(result)
    frame = {
        "plan_id": PLAN_ID,
        "asset_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": results,
    }
    print("HARNESS_PUBLIC_PROBE_V1 " + json.dumps(frame, ensure_ascii=True))
    return (
        2
        if any(case["status"] == "error" for case in results)
        else 1
        if any(case["status"] == "assertion_failed" for case in results)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
