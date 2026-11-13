# Final Verification Report

Date: 2026-11-14 (Asia/Shanghai)

## Release

- Source branch: `feature/opensandbox-hybrid-execution`
- Full-gate release: `520fa21638a3c6f5b55d11e15718953b826985cd7b9ecea4ae5196adbf8a0098`
- Deployment namespace: `sandbox-agent`
- Deployment root: `/data2/wujing03/distributed-agent-sandbox-backend/.sandbox-cluster`
- Service status: 10/10 healthy (two API, two Executor, two Watchdog, two Media, Orchestrator, Observer)
- Full-gate log: `.sandbox-cluster/verification/520fa21638a3-1355c740.log`

Full gate passed: 1,779 Python tests passed, 6 explicit GPU/SAM/OCR smoke tests skipped, and 112 Node tests passed. No gate failures remained in the final release.

## Real Cases

The manifest contains 127 cases: 30 original media cases, 49 dynamic workflow cases, 17 intervention cases, and 31 Native Skill cases.

| Group | Passed | Non-program/model pause | Program errors |
|---|---:|---:|---:|
| C01-C30 | 29 | 1 (`C27`, provider/model authorization pause) | 0 |
| D01-D49 | 48 | 1 (`D46`, model/content review pause after recovery boundary) | 0 |
| R01-R17 | 16 | 1 (`R16`, model clarification after bounded processing) | 0 |
| S01-S31 | 29 | 2 (`S09`, `S13`, model token budget exhausted) | 0 |
| **Total** | **122** | **5** | **0** |

The five non-program outcomes were retained as explicit user-visible pauses with cleanup evidence. They were not converted into false successes and did not trigger case-specific production workarounds.

## General Fixes

- Successor Run handoff now fences planning until carried inputs are applied; consumed input history cannot trigger a `new_input_barrier` loop.
- Initial Provider authorization may fill an otherwise empty delivery contract; later contract changes still require explicit pending user input.
- Native Harness progress events are structured, attempt/revision-bound, and emitted only after a durable output checkpoint whose hashes match the event. Claude command stdout JSON is unwrapped through the protocol envelope; bare text is rejected.
- Final Reviewer is explicitly separated from the user publication checkpoint. Reviewers assess the candidate; the Orchestrator creates the publication decision only after `pass`.
- Publication is fenced while any `ROUTING`, `ACCEPTED`, or `PLANNING` user message remains unresolved.
- Fault-injection isolation distinguishes idle `WAITING_USER` runs from active transitions, and campaign interrupts restore workers, API, Orchestrator, Observer, routes, and objects before exit.
- Async Provider recovery reuses a saved external handle when the Provider is already `succeeded` but the result payload was not yet collected; it never resubmits.
- Browser campaign input submission waits for capability/content refresh and retries the same structured request after a fingerprint refresh.

## Remaining Notes

The report directory keeps every attempt, cleanup record, browser screenshot, internal audit, and model/provider boundary. The generated `dynamic-test-report.md` is a historical evidence index; this file is the consolidated final conclusion. No database migration or source deployment outside the isolated `/data2/wujing03` namespace was performed.
