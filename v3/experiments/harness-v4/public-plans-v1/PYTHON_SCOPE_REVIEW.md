# Three development-only public plans

These assets were authored from the three public development objectives and their
public acceptance text. The pinned public source and declared public tests were
used to confirm the interfaces. No private judge, holdout, gold answer, or
candidate implementation was opened. They are public engineering feedback and
do not replace independent scoring.

`python-failed-task-plans.json` contains the explicit manifest path and SHA-256
for each task. `python-asset-index.json` records each asset, plan, public task
projection and original-tree hash. The executor mounts only
`assets/<asset-sha256>/probe.py` outside the model workspace, read-only, and runs
the plan's fixed `python3 -B` argv. The readable `sources/` copies are byte-identical
to those frozen assets. The task projections deliberately omit controller and
private-scoring metadata.

| Public task | Cases | Public basis and deliberate boundaries |
| --- | --- | --- |
| Click `help` argument | 3 | Positional value reaches the callback; automatic `--help` works before or after a positional value and remains eager. Command construction and imports are setup; failures there are errors, not target assertion failures. |
| Requests `is_http_url` | 12 | Public examples plus absolute HTTP(S) authority, IPv6, userinfo, case, whitespace, non-string, missing-host and invalid-port boundaries stated by the objective. Missing requested API is a target assertion failure. No scheme-less normalization or invented empty-port policy is required. |
| Requests quoted content type | 6 | Semicolon/equal data inside double quotes, independent parameters, unquoted delimiters, media type/name case, quote removal and bare `True` preservation. No new policy for escaped quotes or malformed headers is invented. |

Imports must resolve inside the current project. Import exceptions, including
`AssertionError`, are reported as errors. Unexpected import output cannot create
an extra protocol frame. Each probe emits one JSON frame with its self-computed
asset hash and the exact frozen case IDs; assertion failures exit 1, errors exit
2, and all passed exits 0. Collection, import, timeout, skipped and xfailed cases
cannot masquerade as a valid original red or candidate green.

The original workspaces were produced with the formal materialization stages:
pinned Git object, declared V35 public files, then the V2 public-amendment hook.
Their tree hashes therefore bind the actual controller inputs rather than a
bare checkout. Source caches are the existing public dev caches under
`/home/developer/magr-harness/v3/.local/h12/sources/`; image locks are the existing
`click-less-th0p1e75`, `requests-tests-661970d1` and
`requests-tests-bc7dd0fc` locks. Images and dependencies were not rebuilt.

Original-tree execution used the existing Broker and CommandSession. The exact
pre-existing public regression argv is preserved: Click runs
`tests/test_help_name_public.py`, the URL helper runs `tests/test_utils.py`, and
the quoted-header task runs `tests/`. Both Requests probes were genuine assertion
red; their original regression commands passed 229 and 617 tests respectively.
Click's probe was genuine assertion red, while the first regression attempt was
**NOT_RUN**: pytest attempted cache writes to the read-only workspace and Click's
warning policy promoted the resulting cache warning to an error. The incomplete
assertion report is retained and must not be reclassified as a valid red.

`python-engineering-review.json` binds the full evidence hashes. Raw original
receipts and logs live in the ignored
`evidence/harness-v4/H12-FORMAL-PUBLIC-PLANS-V1/broker-originals/` directory.
The 15 new synthetic asset tests passed; previous public-probe tests were not
rerun. This work used zero model calls and changed no original task, threshold,
resource budget, source implementation, or historical result. Candidate quality
and independent scoring remain **NOT_RUN** for these new plans.
