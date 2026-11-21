"""Offline negative controls for the frozen step protocol and the paid journal."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import httpx
from provider import Ledger, Stopped
from run import Case, Environment, grouped_trials, one_session

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.quality_eval.e3_protocol import Submit, check, expected, snapshot
from mgar_v3.quality_eval.e3_run import Recall


def exercise(directory):
    _, raw, _ = grouped_trials()[0]
    case = Case(**{k: raw[k] for k in ("case_id", "domain", "label", "source", "operation")})
    seed = raw["seed"]
    old = snapshot(case, seed, 1)
    new = snapshot(case, seed, 3)
    answer = Submit(answer=expected(case, old), version=old.version, proof=old.proof)
    assert check(case, new, answer, {old.proof}) == "STALE_VERSION"
    assert check(case, old, answer, set()) == "CURRENT_EVIDENCE_REQUIRED"
    wrong = answer.model_copy(update={"answer": "incorrect"})
    assert check(case, old, wrong, {old.proof}) == "WRONG_ANSWER"
    responses = iter(
        ['{"action":"inspect_current"}', '{"action":"inspect_current"}', answer.model_dump_json()]
    )
    live = Mock()
    live.complete.side_effect = lambda *args: (
        next(responses),
        {"input": 4, "output": 2, "cost": 3},
    )
    env = Mock(spec=Environment)
    env.memory_id = None
    env.recall.return_value = Recall("off")
    env.artifacts = ArtifactStore(directory / "cas")
    env.inspect.return_value = "synthetic-receipt"
    row = one_session(env, case, seed, "off", 1, 1, "control", {old.version: (set(), False)}, live)
    assert row["final_success"] and row["repeat_tool_calls"] == 1 and row["tool_calls"] == 1
    assert (
        row["actions"][1]["error"] == "CURRENT_RECEIPT_ALREADY_AVAILABLE"
        and not row["first_pass_success"]
    )
    assert live.complete.call_args_list[-1].args[1]["allowed_actions"] == ["submit"]
    assert env.inspect.call_count == 1
    live.complete.side_effect = lambda *args: ("invalid json", {"input": 2, "output": 1, "cost": 2})
    row = one_session(env, case, seed, "off", 1, 1, "control", {old.version: (set(), False)}, live)
    assert not row["final_success"] and row["reason"] == "INVALID_MODEL_REPLY"
    sent = []

    def endpoint(req):
        body = json.loads(req.content)
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "max"
        sent.append(body)
        return (
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                },
            )
            if len(sent) == 1
            else httpx.Response(502)
        )

    with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
        for replay in (False, True):
            ledger = Ledger(directory / "ledger.sqlite", "unit", "synthetic-only", client)
            try:
                assert ledger.complete("one", {"test": 1})[0] == "ok" and len(sent) == 1
                if replay:
                    try:
                        ledger.complete("two", {"test": 2})
                    except Stopped as exc:
                        assert str(exc) == "PROVIDER_HTTP_502"
                    else:
                        raise AssertionError("missing usage accepted")
            finally:
                ledger.close()
        ledger = Ledger(directory / "ledger.sqlite", "unit", "synthetic-only", client)
        assert ledger.halted and len(sent) == 2
        assert tuple(
            ledger.db.execute("SELECT state,reserve>0 FROM requests WHERE trial='two'").fetchone()
        ) == ("unknown", 1)
        ledger.close()


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        exercise(Path(tmp))
    print(
        "PASS proof guards, repeat inspection, invalid JSON, grant replay and unknown retention"
    )
