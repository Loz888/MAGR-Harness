"""Rebuild the public baseline from Git, then author R1 development materials."""

import collections
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_memory_context_e1 as original  # noqa: E402

# Three independently searchable properties of each operational handover.
# Each one has current, past, and conflicting retired or unrelated evidence.
TOPICS = [
    ("授权清单", "抽取批次", "召回计数"),
    ("归档副本", "索引重建", "留存周期"),
    ("采纳凭据", "抽检间隔", "复核人数"),
    ("结项台账", "封存时限", "复用配额"),
    ("请求清单", "拼装时限", "预留容量"),
    ("回读收据", "摘要宽度", "日志保留"),
    ("快照副本", "恢复批次", "核验时限"),
    ("源码清单", "分析频次", "摘录宽度"),
    ("报告校样", "校对间隔", "引用配额"),
    ("事件抄本", "刷新间隔", "缓冲容量"),
    ("资料清单", "复访周期", "排队时限"),
    ("字形样本", "打样批次", "页边预留"),
    ("核算底稿", "复算间隔", "归档容量"),
    ("导入底稿", "采样行数", "字段限额"),
    ("步骤清单", "巡检频率", "依赖数量"),
    ("认领回执", "盘点周期", "交接时限"),
    ("校验清单", "抽样比例", "签收人数"),
    ("动作凭据", "对账周期", "交割批次"),
    ("授权台账", "复审周期", "移交人数"),
    ("资料回执", "重查间隔", "引用深度"),
    ("上下文底稿", "刷新周期", "摘要限额"),
]
REV = original.BASE
OLD = original.ARCHIVED_COMMIT


def dump(name, rows):
    raw = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)
    path = HERE / name
    if path.exists() and path.read_bytes() != raw.encode():
        raise ValueError(f"Refusing to replace a different input: {name}")
    path.write_bytes(raw.encode())
    return original.sha(raw.encode())


def build():
    base, overlays, queries, labels = [], [], [], []
    groups = [(p, n) for p, n, _ in original.DEV] + list(original.HOLDOUT)
    for i, (path, topic) in enumerate(groups):
        cohort = f"{i + 1:02d}-{Path(path).stem.replace('_', '-')}"
        full = "src/mgar_v3/" + path
        samples = original.extract(full, cohort, topic, original.pinned_source(full))
        controls = original.controls(cohort, topic, i)
        base.extend([*samples, *controls.values()])
        if i >= len(TOPICS):
            continue
        subject, meter, limit = TOPICS[i]
        props = [
            (subject, "交接介质", "签章纸册", "只读账本", "双联回执"),
            (meter, "标准值", f"{4 + i} 个单位", f"{8 + i} 个单位", f"{2 + i} 个单位"),
            (limit, "上限", f"{38 + i} 个单位", f"{60 + i} 个单位", f"{12 + i} 个单位"),
        ]
        group = []
        for p, (name, noun, live, prior, retired) in enumerate(props):
            for v, (value, purpose) in enumerate(
                [
                    (live, "current_version"),
                    (prior, "historical_commit"),
                    (retired, "requires_reverification"),
                    ("银印底单", "acl_distractor"),
                ]
            ):
                text = (
                    f"{topic}交接档案。{name}条目登记的{noun}是{value}。"
                    f"班组按照该条目完成{subject}的交接。"
                )
                if v == 3:
                    text += f"附带跨组清点编号 KZ-{i + 11}-{p + 7} 的签字意见。"
                item = original.record(
                    cohort,
                    f"handover-{p}-{v}",
                    topic,
                    text,
                    {"kind": "synthetic_control", "purpose": purpose},
                    commit=OLD if v == 1 else REV,
                    lifecycle="stale" if v == 2 else "active",
                    isolate=(p + i) % 3 if v == 3 else None,
                )
                group.append(item)
        overlays.extend(group)
        scoped = [*samples, *controls.values(), *group]

        def ask(category, text, pos=(), commit=REV, cohort=cohort, scoped=scoped):
            query_id = f"r1-dev-{len(queries) + 1:03d}"
            scope = {**original.scope(cohort), "commit": commit, "as_of": original.AS_OF}
            authorized = [
                r
                for r in scoped
                if all(r["draft"][k] == scope[k] for k in ("project_id", "repository", "actor"))
            ]
            relevant = {r["memory_id"]: 3 for r in pos}
            labels.append(
                {
                    "query_id": query_id,
                    "relevance": relevant,
                    "expected_answerable": bool(relevant),
                    "forbidden_ids": [r["memory_id"] for r in scoped if r not in authorized],
                    "invalid_version_ids": [
                        r["memory_id"]
                        for r in authorized
                        if r["intended_lifecycle"] == "stale" or r["draft"]["commit"] != commit
                    ],
                }
            )
            queries.append(
                {
                    "query_id": query_id,
                    "query": text,
                    "category": category,
                    "cohort": cohort,
                    "scope": scope,
                }
            )

        symbols = [
            s
            for r in samples
            for s in r["provenance"]["symbols"]
            if len(s.strip("_")) > 3 and not s.startswith("__")
        ]
        unique = list(dict.fromkeys(symbols))
        for j in range(3):
            name = unique[((len(unique) - 1) * (j + 1)) // 4]
            definitions = [r for r in samples if name in r["provenance"]["symbols"]]
            ask("exact_symbol", f"{topic}的实现中，哪一段源码声明了标识符 `{name}`？", definitions)
        ask("semantic_paraphrase", f"为{topic}换班时，{subject}用什么载体传给下一位？", [group[0]])
        ask("semantic_paraphrase", f"接手{topic}以后，怎么知道{meter}应取多少？", [group[4]])
        ask("semantic_paraphrase", f"准备{topic}下次交接，{limit}最多能给到多少？", [group[8]])
        ask("time_validity", f"{topic}本期的{subject}应以哪种交接介质为准？", [group[0]])
        ask("time_validity", f"清点仍有效的{topic}记录，{limit}现在的边界是多少？", [group[8]])
        ask("commit_version", f"在指定修订上，{topic}的{meter}登记了哪个数？", [group[5]], OLD)
        ask(
            "commit_version",
            f"查所选旧版的{topic}档案，{subject}的交接介质是什么？",
            [group[1]],
            OLD,
        )
        ask("conflict", f"两份{topic}交接单上的{meter}不同，哪份对本次交接仍有约束？", [group[4]])
        ask("conflict", f"移交{topic}时发现{limit}有几个互相矛盾的数，采用哪个？", [group[8]])
        ask("no_answer", f"{topic}在{subject}移交时使用哪个每月审计发票号码？")
        ask("no_answer", f"{topic}上次换班时，谁核准了保险费率 XT-{i + 130}？")
        ask("acl_negative", f"查阅{topic}跨组清点编号 KZ-{i + 11}-7 的签字意见。")
    assert len(queries) == len({re.sub(r"\s+", "", r["query"]) for r in queries}) == 315
    assert len(base) == 1080 and len(overlays) == 252
    assert (
        dump("base-corpus.jsonl", base)
        == "aa8ddd2e5ebc6eece9fa1adbff90715bc185e40616bf344da1e96b066566b45d"
    )
    for file, rows in [
        ("development-overlay.jsonl", overlays),
        ("development.queries.jsonl", queries),
        ("development.labels.jsonl", labels),
    ]:
        print(file, len(rows), dump(file, rows))
    print(collections.Counter(r["category"] for r in queries))


if __name__ == "__main__":
    build()
