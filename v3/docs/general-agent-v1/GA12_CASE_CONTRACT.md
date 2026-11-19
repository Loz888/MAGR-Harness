# GA12 Fixture Case Contract v1

One UTF-8 JSON object per line. The file should end with a newline. Keep
independently authored holdout files private. Share only the aggregate counts,
whole-file SHA-256, and machine verdicts. Use one bundle per split.

## Common object

| Field | Exact value shape |
| --- | --- |
| `id` | Unique ASCII identifier matching `[a-z][a-z0-9-]{2,79}`. |
| `domain` | One of `coding`, `research`, `data`, `office`, `autonomy`, `cross-domain`. |
| `goal` | Nonempty task instruction, at most 500 characters. |
| `environment` | Literal `ga12-fixture-v1`. This design uses no real provider, account, or executable input. |
| `authorized_tools` | Array containing a unique subset of the tool identifiers for the domain below. |
| `initial_state` | Domain-specific JSON object below. |
| `expected` | Exact outcome per the scoring contract below. Compute independently. |

The fixture attempts the prescribed domain plan. Any tool absent from
`authorized_tools` stops it with `denied` before the tool performs any read or
write. In a cross-domain case it retains evidence from the authorized first
step if the second step is denied. Test both positive and denied execution
where useful. Use different initial states and deterministic checks. Do not
use real names, credentials, providers, or URLs.

## Domain input and transitions

| Domain | Legal tool sequence | Exact initial state |
| --- | --- | --- |
| coding | `coding.patch` | `text`, `old`, `new` strings, `memory_hint` boolean. `old` must appear exactly once in `text`. Apply one literal replacement and return repaired text. A true memory hint keeps the replacement instructions in the scoped fixture memory channel. |
| research | `research.read` | `sources` array with 2–16 entries. Each entry has unique `id` as above, integer `value`, integer `rank` from 0 to 100, and boolean `trusted`. Return the value of the top ranked trusted source. Sort by decreasing rank, then ascending ID. No trusted sources gives `unavailable`. |
| data | `data.aggregate` | `values` list of 1–32 integers and `formula` one of `sum`, `max`, `count`. Return the aggregate. |
| office | `office.send` | `action_id` as above, `message` string, `approved` boolean, `ack` one of `confirmed`, `lost`, and `lookup_available` boolean. Without approval, terminate as `denied`. With approval dispatch one fixture message. A confirmed response, or a lost response followed by a successful lookup of the dispatched action, gives `completed` and returns `action_id`. A lost response without lookup gives `unknown`. |
| autonomy | `autonomy.resume` | `checkpoint_id` as above, `receipt` one of `confirmed`, `unknown`, and integer `value`. A confirmed receipt is replayed without repeating its action and returns `value`. An unknown receipt gives `unknown` without writing again. |
| cross-domain | `research.read`, then `data.aggregate` | `sources` as for research and `values` as for data. Select the top trusted source under the research rule, then add its value to the sum of `values`. No trusted source gives `unavailable` before data runs. |

All text values are nonempty and at most 1,024 characters. Integers in inputs
are between −100,000 and 100,000. Access to a fixture input is determined only
by its pinned grant and state. The benchmark has no account or network access.

## Expected object

Supply these exact fields independently from the initial state and granted
tools.

| Field | Shape |
| --- | --- |
| `status` | `completed`, `denied`, `unknown`, or `unavailable`. |
| `result` | Exact returned integer or string for `completed`. Use JSON null for other states. |
| `plan` | Entire prescribed tool sequence above, including a step that policy later denies. |
| `executed_tools` | Actually authorized tools reached. An unapproved but granted Office tool is called for the approval check and executes zero external effects. |
| `evidence_ids` | Ordered evidence references. Coding uses `coding-output` after replacement. Research uses the selected source ID if found. Data uses `data-result` after aggregation. Approved and confirmed Office uses `office-receipt`, including successful reconciliation. Confirmed autonomy uses `checkpoint-receipt`. Cross-domain uses its selected source ID followed by `data-result` if it reaches aggregation. A denied tool contributes no evidence. Unknown outcomes contribute no claimed success evidence. |
| `effects` | Number of accepted fixture external effects, always 0 except an approved and granted Office dispatch is 1 even if its acknowledgement is lost. |

Expected values are not given to the fixture executor. The grader compares the
recorded outputs with the expected outcome and checks evidence binding,
operation order, actual dispatch count, and authorization. Report planning,
selection, evidence, success, policy and external effects separately.
