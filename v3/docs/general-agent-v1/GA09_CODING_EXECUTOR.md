# GA09 Coding Executor

GA09 turns a current TaskGraph claim into an existing Harness CodingLoop run. The `coding.execute.inspect` and `coding.execute` capabilities preserve the original Coding catalog and APIs. The service records the full claim, granted scope, options hash and bounded limits in the existing Harness request. The claim-derived child identifier supports repeated submission and recovery of a queued run. Dispatch, recovery and each protected operation check the current graph, capability pin, permission, deadline and membership. [Executor](../../src/mgar_v3/general_agent/coding_executor.py) · [Harness service](../../src/mgar_v3/harness/service.py)

The local deterministic adapter requires a scoped target and receives no production credential. For bugfix, feature and refactor, the trusted caller supplies explicit approval and the expected replacement input. The run_app policy uses an approved application plan and the existing isolated application lifecycle. Commands continue through the native Broker. Coding reserves the registered action's model and tool allowances in the TaskGraph and metered use stays with the original Harness budget. [Dispatch contract](../../src/mgar_v3/general_agent/coding_executor_contract.py)

Pause and resume reuse the same child task and original Run. Known native results settle the TaskGraph and confirmed failures propagate to dependent nodes. An unresolved native result remains unknown. Inspection and application deliverables are checked against the original successful Harness result, its Run and final tree. Patch and test artifacts use existing GA05 diff and execution checks. The independent GA05 verifier must issue an attestation for the current graph's outputs before publication. [Verification checks](../../src/mgar_v3/general_agent/verification_store.py)

## Verification

```bash
make verify TASK=GA09
```

Engineering fixtures cover inspect, pause and resume, a failure propagated to TaskGraph, queued and settled-model recovery, a real isolated Python bugfix with passing tests, and application readiness and cleanup. Feature and refactor share the approved patch path. The scope check covers all four policies requiring approval.

The focused H11/H12 compatibility run reported 158 passed and three H12 dispatch-gate failures. A detached `fee1914` baseline reproduced those same three `Frozen H00 bytes do not match` failures. The archive and live evaluation were outside this run. The raw reports are in `evidence/GA09`. Production Coding model calls and publication were `NOT_RUN`.
