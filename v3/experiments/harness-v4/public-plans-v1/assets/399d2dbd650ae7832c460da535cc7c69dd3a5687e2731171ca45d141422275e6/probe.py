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
PLAN_ID = "h12-public-requests-http-url-v1"
CASE_IDS = (
    "plain-http",
    "https-port-path",
    "ipv6-authority",
    "userinfo-host",
    "scheme-case",
    "relative-references",
    "other-schemes",
    "whitespace",
    "non-strings",
    "missing-host",
    "invalid-port",
    "malformed-ipv6",
)

CASES = {
    "plain-http": ("http://example.com", True),
    "https-port-path": ("https://example.com:8443/path", True),
    "ipv6-authority": ("http://[::1]:8080/path", True),
    "userinfo-host": ("https://user:pass@example.com/path", True),
    "scheme-case": ("HTTPS://Example.com/path", True),
    "relative-references": (["//example.com", "example.com", "/relative"], False),
    "other-schemes": (["ftp://example.com", "mailto:a@b", "file:///tmp/item"], False),
    "whitespace": (
        [" http://x", "http://x ", "http://exa mple.com", "http://x/line\nbreak", "http://x/\t"],
        False,
    ),
    "non-strings": ([None, True, 4, 1.5, b"http://example.com", [], {}], False),
    "missing-host": (["http://", "http:///path", "https://?q=value", "http://:80"], False),
    "invalid-port": (
        ["http://example.com:not-a-port", "http://example.com:-1", "http://example.com:65536"],
        False,
    ),
    "malformed-ipv6": (["http://[::1", "http://[]/", "http://[example.com]/"], False),
}


def load_target():
    import requests.utils

    if not Path(requests.utils.__file__).resolve().is_relative_to(ROOT.resolve()):
        raise ImportError("requests did not load from the current workspace")
    return requests.utils


def check_case(case_id, target):
    function = getattr(target, "is_http_url", None)
    assert callable(function), "the publicly requested is_http_url API must exist"
    values, expected = CASES[case_id]
    if not isinstance(values, list):
        values = [values]
    for value in values:
        try:
            actual = function(value)
        except Exception as error:
            raise AssertionError("is_http_url must never raise: " + type(error).__name__) from error
        assert actual is expected, "URL classification differs from the public input contract"


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
