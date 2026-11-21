"""Archive observed E3 sessions and the existing read-only grant ledger. No model calls."""

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from summarize import ARMS, summarize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
DEFAULT_EVIDENCE = ROOT / "evidence/CAPABILITY-PHASE2/e3"
CALL_LIMIT, USD_MICROS_LIMIT = 2040, 18_000_000
ACCOUNT = (
    "request_slots",
    "settled_calls",
    "settled_cost_upper_micros",
    "held_reserve_micros",
    "input_tokens_settled",
    "output_tokens_settled",
    "unresolved_slots",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def description(path):
    p = path.resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def pointer(path, data=None):
    if data is None:
        data = path.read_bytes()
    return {"path": description(path), "sha256": digest(data), "bytes": len(data)}


def latest(mode_dir, supplied):
    latest_file = mode_dir / "latest.txt"
    if supplied:
        if supplied.name == "latest.txt" or (
            supplied.is_dir() and (supplied / "latest.txt").exists()
        ):
            latest_file = supplied if supplied.name == "latest.txt" else supplied / "latest.txt"
        elif supplied.name == "raw.jsonl":
            return supplied, "explicit_raw"
        else:
            return supplied / "raw.jsonl", "explicit_attempt"
    if latest_file.exists():
        name = latest_file.read_text().strip()
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError(f"Invalid latest pointer in {latest_file}")
        return latest_file.parent / name / "raw.jsonl", "latest_receipt"
    attempts = [p for p in mode_dir.glob("*") if p.is_dir()]
    if attempts:
        # Do not silently fall back to an older attempt if the newest one has zero sessions.
        chosen = max(attempts, key=lambda p: (p.stat().st_mtime_ns, p.name))
        return chosen / "raw.jsonl", "latest_partial_directory"
    return None, "no_attempt"


def read_mode(mode, path, selection, evidence, freeze):
    meta = {
        "selection": selection,
        "raw": None,
        "attempt": None,
        "receipt_bound": False,
        "trailing_incomplete_bytes": 0,
        "candidate_receipt": None,
    }
    if path:
        meta["attempt"] = description(path.parent)
    rows = []
    if path and path.exists():
        raw = path.read_bytes()
        meta["raw"] = pointer(path, raw)
        if raw and not raw.endswith(b"\n"):
            complete = raw.rfind(b"\n") + 1
            meta["trailing_incomplete_bytes"] = len(raw) - complete
            raw = raw[:complete]
        rows = [json.loads(line) for line in raw.splitlines()]
        meta["raw"]["complete_session_rows"] = len(rows)
    if path and (evidence / mode / "acceptance.json").exists():
        receipt = json.loads((evidence / mode / "acceptance.json").read_bytes())
        link = evidence / mode / "latest.txt"
        expected = "PASS" if mode == "fixture" else "EXECUTED"
        meta["candidate_receipt"] = receipt
        meta["receipt_bound"] = bool(
            link.is_file()
            and (link.parent / link.read_text().strip() / "raw.jsonl").resolve() == path.resolve()
            and receipt.get("freeze") == freeze
            and receipt.get("status") == expected
            and receipt.get("sessions") == len(rows)
        )
    return rows, meta


def read_ledger(path, rows, freeze):
    zero = {k: 0 for k in ACCOUNT}
    by_arm = {a: dict(zero) for a in ARMS}
    states, reasons, models = Counter(), Counter(), Counter()
    session_receipts = defaultdict(list)
    logical = hashlib.sha256()
    freezes = []
    db_exists = path.is_file()
    if db_exists:
        # A single read transaction observes an internally consistent snapshot, including WAL.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            freezes = [r[0] for r in db.execute("SELECT freeze FROM grant_meta ORDER BY freeze")]
            entries = db.execute(
                "SELECT trial, request_id, body_sha, state, reserve, cost, input, "
                "output, reason, model, finish, started, finished, response_sha, provider_id, "
                "completion_id FROM requests ORDER BY trial"
            )
            for r in entries:
                meta = dict(r)
                logical.update(
                    (json.dumps(meta, sort_keys=True, ensure_ascii=False) + "\n").encode()
                )
                split, case, arm, session, ordinal = r["trial"].split("/")
                key = (split, case, arm, int(session))
                state = r["state"]
                if state not in ("reserved", "unknown", "settled") or arm not in ARMS:
                    raise ValueError(f"Unexpected request state/arm for {r['trial']}")
                states[state] += 1
                if r["reason"]:
                    reasons[r["reason"]] += 1
                if r["model"]:
                    models[r["model"]] += 1
                session_receipts[key].append(
                    (int(ordinal), state, r["input"], r["output"], r["cost"])
                )
                a = by_arm[arm]
                a["request_slots"] += 1
                if state == "settled":
                    a["settled_calls"] += 1
                    a["settled_cost_upper_micros"] += r["cost"]
                    a["input_tokens_settled"] += r["input"]
                    a["output_tokens_settled"] += r["output"]
                else:
                    a["unresolved_slots"] += 1
                    a["held_reserve_micros"] += r["reserve"]
    observed = set()
    discrepancies = []
    for row in rows:
        key = (row["split"], row["case_id"], row["arm"], row["session"])
        observed.add(key)
        calls = session_receipts.get(key, [])
        ordered = sorted(calls)
        totals = [len(calls)] + [sum(item[i] for item in calls) for i in (2, 3, 4)]
        row_usage = [
            row[k] for k in ("model_calls", "input_tokens", "output_tokens", "cost_micros")
        ]
        if (
            not calls
            or any(r[1] != "settled" for r in calls)
            or [r[0] for r in ordered] != list(range(1, len(calls) + 1))
            or totals != row_usage
        ):
            discrepancies.append(list(key))
    unfinished = {k: v for k, v in session_receipts.items() if k not in observed}
    for arm, a in by_arm.items():
        arm_successes = sum(r["final_success"] for r in rows if r["arm"] == arm)
        a["observed_session_successes"] = arm_successes
        a["cost_upper_micros_per_observed_success_including_unfinished_settled"] = (
            a["settled_cost_upper_micros"] / arm_successes
            if arm_successes and not a["unresolved_slots"]
            else None
        )
    total = {k: sum(a[k] for a in by_arm.values()) for k in ACCOUNT}
    exposure = total["settled_cost_upper_micros"] + total["held_reserve_micros"]
    return {
        "path": description(path),
        "available": db_exists,
        "grant_meta_freezes": freezes,
        "freeze_matches": freezes == [freeze],
        "sha256_sorted_receipt_metadata_snapshot": (logical.hexdigest() if db_exists else None),
        "receipt_states": dict(states),
        "provider_or_policy_reasons": dict(reasons),
        "models_returned": dict(models),
        "totals": total,
        "combined_exposure_upper_micros": exposure,
        "authorized_calls": CALL_LIMIT,
        "authorized_micros": USD_MICROS_LIMIT,
        "remaining_request_slots": CALL_LIMIT - total["request_slots"],
        "remaining_after_exposure_micros": USD_MICROS_LIMIT - exposure,
        "invoice_actual_micros": None,
        "by_arm": by_arm,
        "completed_session_mismatches": discrepancies,
        "requests_without_completed_session_row": sum(map(len, unfinished.values())),
        "uncompleted_sessions_with_requests": [list(k) for k in sorted(unfinished)],
    }


def preflight(evidence):
    items = []
    for path in sorted(evidence.glob("preflight-*.log")):
        data = path.read_bytes()
        lower = data.lower()
        if b"local_token" in lower and b"too_short" in lower:
            reason = "本地会话凭据长度校验失败"
        elif b"jsondecodeerror" in lower:
            reason = "回执或证据文件解析遇到不完整 JSON"
        elif b"operationalerror" in lower:
            reason = "PostgreSQL 连接中断"
        else:
            reason = "参见本次运行日志"
        items.append({**pointer(path, data), "reason": reason})
    return items


def verified_plan():
    cases = HERE / "cases.jsonl"
    rows = [json.loads(line) for line in cases.read_bytes().splitlines()]
    expected = [
        json.loads(line) | {"split": split}
        for name, split in (("development.jsonl", "development"), ("e3-holdout.jsonl", "holdout"))
        for line in (HERE / name).read_bytes().splitlines()
    ]
    if sorted(rows, key=lambda r: r["case_id"]) != sorted(expected, key=lambda r: r["case_id"]):
        raise ValueError("Case schedule differs from frozen source splits")
    return cases


def read_independent(path, raw_pointer, freeze):
    if not path:
        return {"status": "PENDING", "input": None}
    data = path.read_bytes()
    result = json.loads(data)
    binding = result.get("raw_sha256")
    raw_matches = bool(raw_pointer and binding == raw_pointer["sha256"])
    candidate_matches = result.get("freeze", result.get("freeze_sha256")) == freeze
    disagreements = result.get("disagreements")
    reviewed = (
        isinstance(result.get("scheduled"), int)
        and result["scheduled"] == 160
        and result.get("mode") == "formal"
        and result.get("observed") == result["scheduled"]
        and disagreements in ([], 0)
        and candidate_matches
        and raw_matches
    )
    return {
        "status": "VERIFIED" if reviewed else "PENDING_OR_DISAGREEMENT",
        "input": pointer(path, data),
        "raw_sha256_matches": raw_matches,
        **{k: result.get(k) for k in ("scheduled", "observed", "passed", "stale_submissions")},
        "disagreement_count": len(disagreements)
        if isinstance(disagreements, list)
        else disagreements,
    }


def adjudicate(summaries, metadata, ledger, independent):
    formal = summaries["formal"]
    execution = {}
    for mode, s in summaries.items():
        execution[mode] = (
            "COMPLETE"
            if metadata[mode]["receipt_bound"]
            and not s["missing_trials"]
            and not metadata[mode]["trailing_incomplete_bytes"]
            else "PARTIAL"
            if s["trial_rows"]
            else "NOT_RUN"
        )
    controls = summaries["fixture"]["results"]["all"]["arms"]
    mechanics = execution["fixture"] == "COMPLETE" and all(
        a["final_success"]["rate"] == 1
        and a["stale_accepted"] == 0
        and a["accounting"]["model_calls"]["all_rows_total"] == 0
        for a in controls.values()
    )
    arms = formal["results"]["all"]["arms"]
    evidence_ok = (
        ledger["freeze_matches"]
        and not ledger["completed_session_mismatches"]
        and not ledger["requests_without_completed_session_row"]
        and not ledger["provider_or_policy_reasons"]
        and not ledger["totals"]["unresolved_slots"]
        and ledger["remaining_request_slots"] >= 0
        and ledger["remaining_after_exposure_micros"] >= 0
    )
    positive = []
    worsening = []
    for a in ARMS[1:]:
        pairs = [formal["results"][s]["comparisons"][a] for s in ("development", "holdout")]
        if any(p["significant_cost_increase"] is True for p in pairs):
            worsening.append(a)
        if all(
            p["success_improvement"] == "ESTABLISHED" and p["significant_cost_increase"] is False
            for p in pairs
        ):
            positive.append(a)
    stale = sum((a["stale_accepted"] or 0) for a in arms.values())
    if stale:
        status, claim = "FAIL", "STALE_ACCEPTED"
    elif not formal["trial_rows"] and not ledger["totals"]["request_slots"]:
        status, claim = "NOT_RUN", "REAL_MODEL_NOT_MEASURED"
    elif execution["formal"] != "COMPLETE" or not evidence_ok or not mechanics:
        status, claim = "BLOCKED", "INCOMPLETE_OR_ACCOUNTING_UNRESOLVED"
    elif independent["status"] != "VERIFIED":
        status, claim = "BLOCKED", "AWAITING_INDEPENDENT_REVIEW"
    elif positive:
        status, claim = "PASS", "PAIRED_SUCCESS_IMPROVEMENT_WITHOUT_DETECTED_COST_INCREASE"
    else:
        # A paired interval spanning zero leaves the scientific claim inconclusive.
        status, claim = "INCONCLUSIVE", "MEMORY_BENEFIT_NOT_ESTABLISHED"
    return {
        "status": status,
        "real_model_claim": claim,
        "execution": execution,
        "mechanical_fixture": mechanics,
        "ledger_reconciled": evidence_ok,
        "stale_accepted_observed": stale,
        "arms_meeting_success_and_cost_condition_in_both_splits": positive,
        "arms_with_detected_cost_increase_in_either_split": worsening,
        "independent": independent,
        "release_decision": "PHASE1_STOP_REMAINS_IN_FORCE",
    }


def number(v):
    return "未估计" if v is None else f"{v:,.2f}"


def pct(r):
    return (
        "未估计"
        if r["rate"] is None
        else f"{r['numerator']}/{r['denominator']} ({100 * r['rate']:.1f}%)"
    )


def interval(r, scale=1):
    point = r["estimate"]
    if point is None:
        return "未估计"
    ci = r["ci95"]
    return f"{point * scale:+.2f}" + (
        f" [{ci[0] * scale:+.2f}, {ci[1] * scale:+.2f}]" if ci else " [区间不足]"
    )


def report(summaries, ledger, decision):
    lines = [
        "# E3-R1 跨会话记忆收益",
        "",
        f"真实模型结论为 **{decision['real_model_claim']}**。"
        f"归档状态为 **{decision['status']}**，"
        f"独立持出复核为 {decision['independent']['status']}。",
    ]
    if decision["execution"]["formal"] == "COMPLETE":
        scores = summaries["formal"]["results"]["all"]["arms"]
        if all(v["final_success"]["rate"] == 1.0 for v in scores.values()):
            per_arm = scores["off"]["sessions"]
            lines.extend(
                [
                    "",
                    f"四臂分别完成 {per_arm}/{per_arm} 个会话，"
                    "最终成功率均为 100%，配对差均为零。"
                    "本次任务无法分辨记忆带来的成功率提升。",
                ]
            )
        if all(
            v["repeated_error_rate"]["status"] == "INCONCLUSIVE_LOW_OPPORTUNITIES"
            for v in scores.values()
        ):
            lines.extend(
                [
                    "",
                    "四臂各自的可重复错误机会都低于 20，重复错误率不可判定。"
                    "每成功会话的调用、输入 Token 和费用差异见下表及完整区间。",
                ]
            )
    for mode, title in (("formal", "真实模型"), ("fixture", "机械对照")):
        s = summaries[mode]
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "已归档 "
                + str(s["trial_rows"])
                + "/"
                + str(s["planned_trials"])
                + " 个会话，缺少 "
                + str(len(s["missing_trials"]))
                + " 个。以下指标仅覆盖已经写入本次尝试原始记录的会话。",
                "",
                "| 组别 | 首过 | 最终成功 | 召回 | 命中后采用 | 重复错误 |"
                " 重复工具/读取 | 旧版本尝试/采纳 | 调用/成功 | 输入 Token/成功 |"
                " 费用上界 µUSD/成功 | P50/P95 ms |",
                "| --- |" + " ---: |" * 11,
            ]
        )
        for a, v in s["results"]["all"]["arms"].items():
            acc = v["accounting"]
            err = v["repeated_error_rate"]
            er = (
                ("不可判定 (" + str(err["numerator"]) + "/" + str(err["denominator"]) + ")")
                if err["rate"] is None
                else pct(err)
            )
            rep = v["repeated_tool_calls"]
            lines.append(
                f"| {a} | {pct(v['first_pass_success'])} | {pct(v['final_success'])} | "
                f"{pct(v['retrieval_hit'])} | {pct(v['adoption_given_hit'])} | {er} | "
                f"{rep['numerator']}/{rep['denominator']} | "
                f"{v['stale_attempts']}/{v['stale_accepted']} | "
                f"{number(acc['model_calls']['per_successful_session'])} | "
                f"{number(acc['input_tokens']['per_successful_session'])} | "
                f"{number(acc['cost_micros']['per_successful_session'])} | "
                f"{number(v['duration_ms']['p50'])}/{number(v['duration_ms']['p95'])} |"
            )
        if mode == "formal":
            lines.extend(
                [
                    "",
                    "| 拆分 | 与 Off 对比 | 完整配对链 | 成功率差 pp [95% CI] |"
                    " 每成功会话费用差 µUSD [95% CI] |",
                    "| --- | --- | ---: | ---: | ---: |",
                ]
            )
            for split in ("development", "holdout", "all"):
                for arm, c in s["results"][split]["comparisons"].items():
                    changes = c["paired_on_minus_off"]
                    lines.append(
                        f"| {split} | {arm} | {c['matched_case_clusters']} | "
                        f"{interval(changes['final_success_rate'], 100)} | "
                        f"{interval(changes['cost_micros_per_success'])} |"
                    )
    t = ledger["totals"]
    lines.extend(
        [
            "",
            "## 正式请求与预算",
            "",
            (
                f"独立账本占用 {t['request_slots']:,}/{CALL_LIMIT:,} 个请求名额，"
                f"其中 {t['settled_calls']:,} 笔有用量，"
                f"{t['unresolved_slots']} 笔尚未结算。"
                f"已知输入/输出为 {t['input_tokens_settled']:,}/"
                f"{t['output_tokens_settled']:,} Token。按冻结费率计算的已结算费用保守上界为 "
                f"US${t['settled_cost_upper_micros'] / 1e6:.6f}，"
                f"未决预占 US${t['held_reserve_micros'] / 1e6:.6f}。"
                "在 US$18 授权中扣除两项后余 "
                f"US${ledger['remaining_after_exposure_micros'] / 1e6:.6f}，"
                f"账本剩余 {ledger['remaining_request_slots']:,} 个请求名额。供应商发票实扣未知。"
                if ledger["available"]
                else "未发现正式账本文件，本次无法核对已用名额、费用与余额。"
            ),
            "",
            f"有 {ledger['requests_without_completed_session_row']} 个请求缺少已完成会话行。"
            "每成功会话的配对区间仅使用两臂均跑完的链。账本按臂统计保留中断会话内的请求和预占。"
            "固定策略的机械会话调用为零，独立于真实模型统计。",
            "",
            "[指标和完整配对区间](metrics.json) · [原始记录与快照哈希](raw.jsonl) · "
            "[失败与中断记录](failures.md) · [判定](acceptance.json)",
        ]
    )
    audit = decision.get("delivery_and_receipt_audit")
    if audit and audit["binding_matches_this_run"]:
        checked = audit["result"]
        lines.extend(
            [
                "",
                f"独立核验覆盖 {checked.get('delivery_artifacts_verified')} 个交付物和 "
                f"{checked.get('unique_settled_completions')} 份唯一已结算回执，"
                f"状态为 {checked.get('status')}。[字节与账本核验](receipt-audit.json)",
            ]
        )
        estimate = checked.get("off_peak_estimated_nanodollars")
        if estimate is not None:
            covered = checked["receipt_priced_rows"]
            lines.extend(
                [
                    "",
                    f"按 {covered:,} 份供应商回执的缓存命中、未命中和 UTC 时段，"
                    f"套用公开费率复算为 **US${estimate / 1e9:.9f}**。"
                    "该估算用于解释成本，预先冻结的配对成本区间仍以保守上界计算。"
                    f"[费率来源]({checked['public_rate_source']})",
                    "",
                    "| 组别 | 全组费用保守上界 US$ | 按回执和时段估算 US$ |",
                    "| --- | ---: | ---: |",
                ]
            )
            for arm in ARMS:
                upper = ledger["by_arm"][arm]["settled_cost_upper_micros"]
                amount = checked["off_peak_estimated_nanodollars_by_arm"][arm]
                lines.append(f"| {arm} | {upper / 1e6:.6f} | {amount / 1e9:.9f} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE)
    ap.add_argument("--out", type=Path, default=HERE)
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--formal", type=Path)
    ap.add_argument("--ledger", type=Path)
    ap.add_argument("--independent", type=Path)
    args = ap.parse_args()
    plan = verified_plan()
    frozen = HERE / "manifest.json"
    freeze = digest(frozen.read_bytes())
    bindings = json.loads(frozen.read_bytes())["files"]
    for name in ("summarize.py", "development.jsonl", "e3-holdout.jsonl"):
        path = HERE / name
        if digest(path.read_bytes()) != bindings[description(path)]:
            raise ValueError("Frozen scoring input changed: " + name)
    metadata, summary, data = {}, {}, {}
    for mode in ("fixture", "formal"):
        path, selection = latest(args.evidence_root / mode, getattr(args, mode))
        rows, meta = read_mode(mode, path, selection, args.evidence_root, freeze)
        data[mode] = rows
        metadata[mode] = meta
        summary[mode] = summarize(rows, [plan])
    ledger = read_ledger(
        args.ledger or args.evidence_root / "formal/ledger.sqlite", data["formal"], freeze
    )
    independent_path = args.independent or HERE / "independent-formal.json"
    independent = read_independent(
        independent_path if (args.independent or independent_path.exists()) else None,
        metadata["formal"]["raw"],
        freeze,
    )
    decision = adjudicate(summary, metadata, ledger, independent)
    audit_path = HERE / "receipt-audit.json"
    audit = None
    if audit_path.exists():
        audit_data = audit_path.read_bytes()
        audit = json.loads(audit_data)
        audit_bound = bool(
            metadata["formal"]["raw"]
            and audit.get("raw_sha256") == metadata["formal"]["raw"]["sha256"]
        )
        audit = {
            "binding_matches_this_run": audit_bound,
            "file": pointer(audit_path, audit_data),
            "result": audit,
        }
        if audit_bound and audit["result"].get("status") != "PASS":
            decision.update(status="BLOCKED", real_model_claim="RECEIPT_OR_CAS_AUDIT_FAILED")
    decision["delivery_and_receipt_audit"] = audit
    logs = preflight(args.evidence_root)
    failures = ["# E3-R1 预检、会话失败与缺测", ""]
    failures.extend(f"- {i['path']}，{i['reason']}，SHA-256 `{i['sha256']}`。" for i in logs)
    for mode, rows in data.items():
        failures.extend(
            [
                "",
                f"## {mode}",
                "",
                f"缺少 {len(summary[mode]['missing_trials'])} 个计划会话，"
                "全部缺失键保存在 metrics.json。",
            ]
        )
        for r in rows:
            errors = [a["error"] for a in r["actions"] if a.get("error")]
            if not r["final_success"] or errors or r["stale_attempts"]:
                failures.append(
                    f"- {r['case_id']} / {r['arm']} / S{r['session']}，最终成功 "
                    f"{r['final_success']}，会话原因 {r['reason']}，动作错误 {', '.join(errors)}，"
                    f"旧版本尝试 {r['stale_attempts']}。"
                )
    failures.extend(
        [
            "",
            "账本未决或拒绝原因按已观察状态汇总。",
            "",
            json.dumps(ledger["provider_or_policy_reasons"], ensure_ascii=False),
            "",
            f"未决请求 {ledger['totals']['unresolved_slots']}，已完成会话账本不符 "
            f"{len(ledger['completed_session_mismatches'])}，缺完整会话的请求 "
            f"{ledger['requests_without_completed_session_row']}。",
        ]
    )
    collected = datetime.now(UTC).isoformat()
    metrics = {
        "manifest_sha256": freeze,
        "snapshot_utc": collected,
        "source": metadata,
        "budget": ledger,
        "additional_receipt_pricing_and_cas_audit": audit,
        "preflight_attempts": logs,
        **summary,
    }
    pointers = [{"mode": m, **v} for m, v in metadata.items()] + [
        {
            "kind": "sqlite_logical_snapshot",
            "path": ledger["path"],
            "sha256_sorted_receipt_metadata": ledger["sha256_sorted_receipt_metadata_snapshot"],
        },
        {"kind": "frozen_schedule", **pointer(plan)},
        {"kind": "candidate_manifest", **pointer(frozen)},
        *({"kind": "preflight", **r} for r in logs),
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.out) as tmp:
        staging = Path(tmp)
        (staging / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        (staging / "raw.jsonl").write_text(
            "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in pointers)
        )
        (staging / "report.md").write_text(report(summary, ledger, decision))
        (staging / "failures.md").write_text("\n".join(failures) + "\n")
        decision.update(
            snapshot_utc=collected,
            manifest_sha256=freeze,
            files={p.name: digest(p.read_bytes()) for p in sorted(staging.iterdir())},
        )
        (staging / "acceptance.json").write_text(
            json.dumps(decision, ensure_ascii=False, indent=2) + "\n"
        )
        for name in ("metrics.json", "raw.jsonl", "report.md", "failures.md", "acceptance.json"):
            os.replace(staging / name, args.out / name)
    print(
        json.dumps(
            {
                "status": decision["status"],
                "execution": decision["execution"],
                "output": str(args.out),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
