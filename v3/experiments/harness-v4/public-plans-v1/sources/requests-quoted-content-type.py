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
PLAN_ID = "h12-public-requests-quoted-content-type-v1"
CASE_IDS = (
    "quoted-semicolon-equals",
    "independent-quoted-parameters",
    "unquoted-separators",
    "media-type-and-name-case",
    "quote-removal",
    "no-parameters",
)

CASES = {
    "quoted-semicolon-equals": (
        'text/plain; note="alpha;bravo=charlie"; charset=utf-8',
        ("text/plain", {"note": "alpha;bravo=charlie", "charset": "utf-8"}),
    ),
    "independent-quoted-parameters": (
        'multipart/form-data; BOUNDARY="a;b=c"; X="d=e;f"; flag',
        ("multipart/form-data", {"boundary": "a;b=c", "x": "d=e;f", "flag": True}),
    ),
    "unquoted-separators": (
        "text/plain; a=one; b=two; no_equals",
        ("text/plain", {"a": "one", "b": "two", "no_equals": True}),
    ),
    "media-type-and-name-case": (
        'Text/Custom; CHARSET="UTF-8"; token; n=plain',
        ("Text/Custom", {"charset": "UTF-8", "token": True, "n": "plain"}),
    ),
    "quote-removal": (
        'application/json; q=""; value="one=two=three"',
        ("application/json", {"q": "", "value": "one=two=three"}),
    ),
    "no-parameters": ("application/json", ("application/json", {})),
}


def load_target():
    import requests.utils

    if not Path(requests.utils.__file__).resolve().is_relative_to(ROOT.resolve()):
        raise ImportError("requests did not load from the current workspace")
    return requests.utils


def check_case(case_id, target):
    function = getattr(target, "_parse_content_type_header", None)
    assert callable(function), "the existing parser must remain available"
    value, expected = CASES[case_id]
    assert function(value) == expected, "header parsing differs from the public parameter contract"


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
