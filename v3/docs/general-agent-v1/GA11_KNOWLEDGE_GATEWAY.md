# GA11 Cross-domain Context & Knowledge Gateway

GA11 provides read-only discovery, search, read, and citation at `/api/v3/general/tasks/{task_id}/knowledge`. A General Task selects a source in its current `source_refs` using `kind=document`, `reference=knowledge:<source_id>`, and the exact descriptor `version`. The gateway checks the authenticated account, current task revision, project membership, source ACL, and requested version before invoking the source. Each source publishes its domain, search and read tool identifiers, citation schema, freshness window, and trust label.

| Source | Version | Scope and retrieval |
| --- | --- | --- |
| `technical-docs` | `1` | The R4 technical index uses the active RAG snapshot for the caller's R4 actor and filters document ACLs before retrieval. Available to the configured instance project and explicitly enabled projects. Chunking, embedding, and reranking follow the R4 configuration. |
| `project-memory` | `1` | The signed-in account's active project facts and verified experiences in the task project. The adapter uses BM25 by default and accepts its own embedder when configured in Python. |

The instance owner may enable `technical-docs` for other managed projects by setting `MGAR_V3_KNOWLEDGE_GATEWAY_PROJECTS` to a JSON array of project IDs. R4 actor, snapshot, and document ACLs apply. For native extensions, inject a `KnowledgeAdapter` with `Engine(..., general_knowledge_sources=(...))`. Its search and read methods operate on the authorized principal and return typed backend results. The adapter owns its corpus, transport, chunking, and ranking. The gateway and the planner communicate through the existing task source references. [Gateway](../../src/mgar_v3/general_agent/knowledge_gateway.py) · [Native adapters](../../src/mgar_v3/general_agent/knowledge_native.py)

## Task API

1. Include a source reference such as `{"kind":"document","reference":"knowledge:project-memory","version":"1"}` when creating or revising the task.
2. Use `GET /sources?expected_revision=1` to inspect the sources available to the current task. For `project-memory`, use a signed-in browser account. Browser POST requests use the existing Origin and CSRF checks.
3. Use `POST /search` with `{"expected_revision":1,"source_ids":["project-memory"],"query":"release calendar"}`. The reply gives each source's status and ranker metadata. Hits include URI, version, locator, source timestamp, observation timestamp, content hash, and a short-lived read ticket. `max_context_bytes` bounds the total snippet bytes across sources.
4. Pass `expected_revision` and `read_ticket` to `POST /read`. Use `POST /cite` with the same fields plus `excerpt` to verify an exact quote and get a versioned citation. The ticket expires in ten minutes and is bound to the task revision, caller, source descriptor, and location. Reads revalidate authorization, pinned source version, freshness, and content hash. Project memory reads require the latest memory revision.

Search reports `denied`, `stale`, `timeout`, or `unavailable` per affected source and returns usable results from other sources. Retrieved passages are tagged `untrusted_reference`. Citation metadata identifies the concrete source bytes for downstream evidence verification. Any durable GA05 observation follows its own write and validation flow. [API](../../src/mgar_v3/general_agent/knowledge_gateway_api.py)

## Verification

Run `make verify TASK=GA11` with the project PostgreSQL and Redis test services. The suite covers both native sources, different embedding models, scope before search, authentication and CSRF, ACL revocation after search, stale or unavailable sources, read ticket binding, source byte changes, and exact citations. The technical RAG test uses fixture embedding weights. Remote knowledge services and production embedding models require separate integration evidence.
