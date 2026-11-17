# Harness P0 acceptance — 2026-11-17

P0 acceptance passed on the integrated PR #22 branch. This is acceptance of the
Main execution chain, not a claim that the branch has merged or that P1 crash
recovery, real-model quality, or the whole legacy repository has passed.

The production code is unchanged from `7afc70bd`; this follow-up adds acceptance
tests and this record. The same run combines real HTTP/API and Worker processes
over PostgreSQL with database concurrency tests and deterministic runtime
boundary tests. Only `ScriptedModelAdapter` is used, as allowed by the P0 spec.

## Result and replay

**442 passed, 0 failed, 0 errors, 0 skipped**, in 69.86 seconds. This includes
Harness unit/scenario/integration tests and packaging metadata checks. The
cross-process file has seven cases: six business cases plus an inherited
`PYTHONPATH` regression test.

Environment: WSL Ubuntu 22.04, Python 3.11.15, pytest 9.0.2, SQLAlchemy 2.0.48,
asyncpg 0.31.0, FastAPI 0.133.1, uvicorn 0.41.0; a temporary `postgres:16`
container. API and Worker use distinct processes and working directories, with
shared artifact files. The temporary database disables fsync for test speed;
API restart checks prove application-level persistence, not power-loss recovery.

From the repository root, activate an environment containing the locked Harness
dependencies and pytest, then run:

```bash
python -m pytest tests/harness_runtime tests/test_packaging_metadata.py \
  -q -n0 -o addopts="" -o junit_family=xunit1 \
  --junitxml=harness-acceptance.xml
```

`-o addopts=""` includes integration tests, which the repository normally
excludes. `-n0` avoids one PostgreSQL container per xdist worker. With Docker
available and `DARAOS_HARNESS_TEST_DSN` unset, the fixture creates and removes its
own database. If a test DSN is supplied instead, it must refer to a disposable
test database: the fixture clears its tables.

Six business cases emit an `acceptance_evidence` JUnit property containing
actual request bodies, scripted steps, HTTP responses, task state, trace steps
and result/error content. This does not include auth headers, tokens or DSNs.
Test-local paths and generated IDs are observations; rerunning the fixtures
creates new paths and IDs.

## Acceptance matrix

Paths below are relative to `tests/harness_runtime/`. Every listed case was
included in the passing run above.

| Requirement | Observable result | Tests and evidence layer |
|---|---|---|
| Main submits, reads, drafts, verifies and returns | Both file actions succeed; actual brief text reaches the next model input; verified draft is returned over HTTP | `integration/test_processes.py::test_api_and_worker_share_tasks_across_processes[foreground]` — real API + PG + Worker |
| Role and lane stay independent | `main/foreground` and `main/background` persist in task/trace and survive API restart | Same case parameterized with `background`; `scenarios/test_worker.py::test_worker_only_takes_its_lanes` |
| Idempotency and conflict | Repeating the request after API restart returns the same task; changed content returns 409; concurrent creation produces one task | Same process case; `integration/test_postgres.py::test_same_request_key_created_once_across_connections` |
| Authentication and scope isolation | Missing token is 401; another scope gets 404 for task, trace, SSE, artifact and cancellation | Same process case; `scenarios/test_api.py::test_identity_and_role_cannot_come_from_the_body` rejects body-supplied identity |
| Unauthorized file reads are refused | `files.read` returns denied/forbidden; no read action is registered; every subsequent model input excludes the private fixture content | `integration/test_processes.py::test_worker_refuses_outside_file_without_exposing_its_content` — real API + PG + Worker; API input-path rejection also covered in `scenarios/test_api.py` |
| Budget is enforced | A script requesting ten calls stops at three model calls with `budget_exhausted`; only two tool actions occur, with wrap-up and reminder events | `integration/test_processes.py::test_worker_stops_at_model_budget_and_exposes_failure`; token/tool/deadline boundaries in `unit/test_runner.py` |
| Claims and budget reservations are atomic | Separate connections cannot claim one task twice or reserve the same last budget unit | `integration/test_postgres.py::test_concurrent_claims_from_separate_connections`, `test_last_budget_unit_goes_to_one_caller` |
| Old or expired leases cannot commit | Old epoch loses to the current Worker; expired lease writes raise `StaleLease`; a Worker that loses its lease commits nothing | `integration/test_postgres.py::test_old_and_new_worker_race_to_commit`; postgres variant of `unit/test_repositories.py::test_expired_lease_is_rejected`; `scenarios/test_worker.py::test_lost_lease_commits_nothing` |
| Cancellation stops new work | Queued cancellation leaves zero calls and is not claimed by another process; runtime cancellation and commit boundaries preserve the winning terminal state | `integration/test_processes.py::test_cancelled_queued_task_is_not_claimed_by_another_process`; `scenarios/test_worker.py::test_cancel_during_the_run_ends_cancelled_without_more_tools`, `test_cancel_at_completion_boundary_finishes_attempt`; both variants of `integration/test_postgres.py::test_cancel_and_completion_resolve_under_the_task_lock` |
| Verification gates success | Missing Plan section yields failed/`verification_failed`, no published result_ref; partial draft remains readable | `integration/test_processes.py::test_failed_verification_keeps_the_draft_without_publishing_a_result`; unreadable/foreign/missing artifacts and inconclusive outcomes in `unit/test_verifier.py` |
| SSE and persisted results agree | Final follows committed success; Last-Event-ID resumes without gaps; a restarted API reads the same result, trace and idempotency key | Successful process cases; `integration/test_postgres.py::test_event_cursor_does_not_skip_a_late_commit` |

Running cancellation and stale-lease races use runtime and real-PG transaction
tests, where their ordering can be controlled. They are not represented as
operating-system crash recovery or as every race having crossed a live HTTP
boundary.

## Observed input, output and error classification

The normal request asks `Write a project draft from the brief`, uses
`document_draft@1`, references the fixture `brief.md`, and requires readable
`draft` output with Goal and Plan sections. The input file contains:

```text
Read this brief and write a project draft.
```

The scripted model requests `files.read`, then `artifacts.write`, then submits
the resulting artifact. The actual second model input contains that brief in
the `files.read` tool reply with `status: succeeded`. The actual HTTP artifact
response is:

```markdown
# Draft
## Goal
Read the brief.
## Plan
Run the harness.
```

| Observed case | HTTP create | Terminal task / tool outcome | Model / tool calls |
|---|---|---|---|
| Main, foreground | 202 | succeeded, readable verified draft | 3 / 2 |
| Main, background | 202 | succeeded, role remains main | 3 / 2 |
| Cancel before Worker | 202; cancel 200 | cancelled; Worker reports `claimed: null` | 0 / 0 |
| Read outside whitelist | 202 | read denied: `(forbidden): path is outside the directories this user allows`; later authorized draft succeeds | 3 / 1 |
| Request ten reads with max_model_calls=3 | 202 | failed; `budget_exhausted`, `retryable: false`, no result_ref | 3 / 2 |
| Draft omits Plan | 202 | failed; `verification_failed`, `retryable: false`, no result_ref; partial artifact HTTP 200 | 3 / 2 |

Budget failure message: `budget used up (max_model_calls) before a result was returned`.
Verification failure message: `result did not pass verification: required_sections: 'draft' is missing: Plan`.

The permission case deliberately permits later authorized work: a denied tool
call is not automatically a failed task. Its fixed scripted draft is evidence
of policy enforcement and continuation, not factual quality based on denied
material. The retained incomplete draft is:

```markdown
# Draft
## Goal
The required Plan section is missing.
```

Raw run files are retained in the task workspace under
`outputs/p0-acceptance-20261117/`: `harness-acceptance.log`,
`harness-acceptance.xml`, and `p0-observations.json` (six actual business-case
records extracted from that JUnit report). The development record carries the
same result and observations. These local run files are not runtime inputs.

## Repository-wide check and limits

The required pre-push run, `python -m pytest tests/ -q -n4` with an isolated
`DACLAW_HOME`, produced **12,490 passed / 40 failed / 36 skipped / 10 errors** in
360.61 seconds. Its 372 Harness cases had no failures. The repository-wide
suite is not green; all failures are outside Harness, and all ten errors come
from the Discord reply-mode fixtures.

Four `test_send_message_tool.py` failures not seen in the two immediately prior
recorded runs expected mirror `source_label="cli"` but observed an empty string.
Those four cases pass both here (2.61 seconds) and on clean `7afc70bd` (2.67
seconds), using the same environment and separately isolated homes. Relevant
production and legacy test files are unchanged. This gives no evidence of a
regression from the acceptance changes, but does not identify or resolve the
full-suite context/order interaction. The two targeted logs and JUnit files
are retained alongside `full-suite.log` and `full-suite.xml`.

P1 remains responsible for expired-lease scheduling after a Worker process dies,
checkpoint replay/resume, approvals and unknown-side-effect reconciliation.
Proactive subscriptions, delivery and foreground/background capacity isolation
remain P2. No paid model was called. No existing server service was switched;
this follow-up changes tests and documentation only.
