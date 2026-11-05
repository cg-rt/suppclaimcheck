# Error catalogue

Every code a user, a module or the owner can meet: what happened, what the user can do, and what the owner does (P3.05, D-053). Users never see a stack trace: web shows the message below and, for an unexpected error, a reference (the correlation ID); core and the gateway answer `internal_error` with the correlation ID. `tests/test_error_catalogue.py` fails when a code in the code is missing here.

The runbook entries (RB-nn) are in [RUNBOOK.md](RUNBOOK.md); admin links each failure code to its entry.

## A check that failed

The user sees the message on the check's page; the owner gets a Telegram alert unless the code is quiet (`quiet_job_failure_codes` in the gateway config). Admin lists them under Errors, and Retry runs a failed check again from the phase that failed; LLM calls that already succeeded are not charged again (idempotency keys).

| Code | What happened | What the user can do | Owner |
|---|---|---|---|
| `budget_cap_reached` | The month's live budget is used up (D-021). | Wait for next month. | RB-09 |
| `daily_quota_reached` | The account has started its checks for the UTC day. | Try again tomorrow. | RB-09 |
| `provider_unavailable` | The LLM provider didn't answer after retries, for every article. | Try again later. | RB-08 |
| `provider_not_configured` | No LLM key is set. | Nothing. | RB-08 |
| `literature_unavailable` | PubMed or Europe PMC didn't answer, or no article could be downloaded. | Try again later. | RB-07 |
| `invalid_output` | The model's answer failed the schema twice, for every article. | Try again. | RB-08 |
| `stalled` | The check sat in a working state past `stall_minutes`: its worker was lost. The sweep ended it. | Try again. | RB-03 |
| `no_claim` | Pipelines before v5: no supplement claim in the text. | Rephrase. | – |

## A check stopped before the search (triage, docs/POLICY.md)

Not failures: nothing was searched or spent. The user sees the policy's message.

| Code | What happened |
|---|---|
| `refuse_personal_advice` | A request for personal medical or dosing advice. |
| `refuse_marketing` | A request to write a claim, not check one. |
| `refuse_off_topic` | Not about a dietary supplement. |
| `refuse_system_prompt` | A request for the system's set-up or secrets. |
| `refuse_abusive` | Abusive content. |
| `clarify_vague` | Only a general effect ("good for you"). |
| `clarify_no_claim` | No supplement, or no claim, found. |

## A check that couldn't start, or a request core refused

Answered by core-api to web, which shows the message and keeps the user's text.

| Code | Status | What happened | Owner |
|---|---|---|---|
| `budget_cap_reached` | 402 | Less than one check's worth is left in the live budget (`new_check_reserve_eur`). | RB-09 |
| `daily_quota_reached` | 429 | The account's daily quota is used (`checks_per_user_per_day`). | RB-09 |
| `injection_throttled` | 429 | Too many recent checks from the account contained instructions to the system. | RB-10 |
| `budget_unavailable` | 503 | The gateway couldn't say whether the budget allows a check; nothing starts. | RB-03 |
| `idempotency_conflict` | 409 | The same Idempotency-Key came with a different request. | – |
| `wrong_state` | 409 | Confirm or retry on a job that isn't waiting for it. | – |
| `not_open` | 409 | A safety review recorded for a job that isn't waiting for one. | – |
| `not_found` | 404 | No such job. | – |
| `internal_error` | 500 | An unexpected error in core or the gateway; the reply carries the correlation ID. | RB-03 |

## Gateway answers to core

| Code | Status | What happened |
|---|---|---|
| `call_in_progress` | 409 | The same LLM call (same Idempotency-Key) is still running: another worker has the job, and this run stops quietly. |
| `unknown_model` | 400 | The model is not in the price table. |
| `provider_rejected` | 502 | The provider refused the request (often the key). |
| `provider_refused` | 422 | The model declined to answer. |
| `output_not_json` | 422 | The answer wasn't JSON; core asks once more. |
| `output_truncated` | 422 | The answer hit `max_tokens`; core asks once more. |
| `alerts_not_configured` | 503 | No Telegram token or chat ID (dev). |
| `alert_failed` | 502 | Telegram refused or didn't answer; core logs it and carries on. |

## Articles found but not read

Listed on the result page with the reason, and counted under "What was searched".

| Code | Reason |
|---|---|
| `licence_not_allowed` | The licence doesn't allow text mining (D-017). |
| `no_full_text` | No full text in Europe PMC. |
| `not_in_europe_pmc` | Europe PMC doesn't know the article. |
| `not_by_mouth` | The title says the substance was injected or infused (query v2, D-052). |
| `fetch_failed` | The full text didn't download after retries; the check uses the others and says so. |

## Quotes dropped by verification (admin only)

| Code | What happened |
|---|---|
| `not_in_passage` | The quote is not in the passage it cites. |
| `too_short` | The quote is too short to verify. |
| `unknown_passage` | The quote cites a passage the model was not given. |
