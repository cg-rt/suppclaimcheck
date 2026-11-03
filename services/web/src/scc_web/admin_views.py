"""What the admin pages show (P3.08-P3.10): names for states and error codes, runbook links, numbers.

Every failure type links to the runbook entry that says what to do (D-035). The runbook is linked on
GitHub, not rendered here; tests/integration/test_admin_pages.py checks every anchor still exists.
"""

import json
import re
import uuid
from datetime import datetime

from scc_common import config_store

REPO = "https://github.com/cg-rt/suppclaimcheck"
RUNBOOK = f"{REPO}/blob/main/docs/RUNBOOK.md"
EVALS = f"{REPO}/blob/main/docs/EVALS.md"

# GitHub's anchor for each runbook heading used here.
RB = {
    "RB-01": "rb-01-tunnel-down",
    "RB-03": "rb-03-container-stuck-in-a-restart-loop",
    "RB-04": "rb-04-database-down",
    "RB-07": "rb-07-pubmed-or-full-text-source-down",
    "RB-08": "rb-08-llm-provider-errors-or-rate-limits",
    "RB-09": "rb-09-budget-cap-reached",
    "RB-10": "rb-10-suspected-abuse-or-injection-attempts",
    "RB-15": "rb-15-change-activate-or-roll-back-a-config-version",
}

# Failure codes from core (job failures, dropped quotes, queue tasks) and the gateway (LLM calls).
CODES = {
    "budget_cap_reached": ("The monthly budget cap refused the call", "RB-09"),
    "provider_unavailable": ("The LLM provider did not answer after retries", "RB-08"),
    "provider_not_configured": ("No LLM API key is configured", "RB-08"),
    "provider_rejected": (
        "The LLM provider rejected the call (often the key)",
        "RB-08",
    ),
    "provider_refused": ("The model declined to answer", "RB-08"),
    "output_not_json": ("The model's reply was not JSON", "RB-08"),
    "output_truncated": ("The model's reply hit max_tokens", "RB-08"),
    "invalid_output": ("The model's answer failed the schema twice", "RB-08"),
    "unknown_model": ("The model is not in the gateway's price table", "RB-15"),
    "literature_unavailable": ("PubMed or Europe PMC did not answer", "RB-07"),
    "no_claim": (
        "No supplement claim in the text (the user's input, not a fault)",
        None,
    ),
    "not_in_passage": ("The quote is not in the passage it cites", None),
    "too_short": ("The quote is too short to verify", None),
    "unknown_passage": ("The quote cites a passage the model was not given", None),
    "core:extract_claim": ("The extraction task crashed; the job is stuck", "RB-03"),
    "core:run_pipeline": ("The pipeline task crashed; the job is stuck", "RB-03"),
}

KINDS = {
    "job_failed": "Job failed",
    "job_stalled": "Job stalled",
    "quote_dropped": "Quote dropped",
    "task_failed": "Queue task failed",
    "llm_failed": "LLM call failed",
}

STATUSES = {
    "in_progress": "In progress",
    "awaiting_confirmation": "Awaiting confirmation",
    "done": "Completed",
    "failed": "Failed",
    "refused": "Refused",
}
# The ink each status is written in: a coloured word with a drawn mark, never a filled badge.
STATUS_INK = {
    "in_progress": "ink",
    "awaiting_confirmation": "mixed",
    "done": "supported",
    "failed": "contradicted",
    "refused": "none",
}

# How to find a job's log lines on the VM: every line carries its correlation ID (AGENTS.md section 7).
LOG_COMMAND = "docker compose logs --since 7d --no-log-prefix web core-api core-worker gateway | grep {pattern}"


def log_command(ids: list[str]) -> str:
    """The grep for every correlation ID a job ran under (the submission and the confirmation)."""
    return LOG_COMMAND.format(
        pattern=ids[0] if len(ids) == 1 else "-E '" + "|".join(ids) + "'"
    )


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def code_of(error: str | None) -> str | None:
    """The code at the start of a stored error, such as `provider_unavailable: ReadTimeout`."""
    if not error:
        return None
    match = re.match(r"[a-z0-9_:]+", error)
    return match.group(0).rstrip(":") if match else None


def is_job(value) -> bool:
    """Whether a job reference is a core job ID (eval runs record other names in the gateway)."""
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def rb_link(entry: str | None) -> str | None:
    return f"{RUNBOOK}#{RB[entry]}" if entry in RB else None


def explain(code: str | None) -> dict:
    """What a failure code means and which runbook entry handles it."""
    meaning, entry = CODES.get(code or "", (None, None))
    return {"code": code, "meaning": meaning, "rb": entry, "rb_url": rb_link(entry)}


def eur(value: float | None, places: int = 4) -> str:
    return "–" if value is None else f"€{value:,.{places}f}"


def ms(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"


def seconds(value: float | None) -> str:
    if value is None:
        return ""
    if value < 60:
        return f"{value:.1f} s"
    minutes, rest = divmod(int(value), 60)
    return (
        f"{minutes} min {rest} s"
        if minutes < 60
        else f"{minutes // 60} h {minutes % 60} min"
    )


def when(value: str | datetime | None) -> str:
    """UTC date and time to the second, as the logs write it."""
    if value is None:
        return ""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


def diff_lines(old_toml: str | None, new_toml: str, old: str, new: str) -> list[dict]:
    """The unified diff from config_store.diff, one row per line with its kind for styling."""
    rows = []
    for line in config_store.diff(old_toml, new_toml, old, new).splitlines():
        if line.startswith(("+++", "---")):
            kind = "file"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "remove"
        else:
            kind = "same"
        rows.append({"kind": kind, "text": line})
    return rows


def thresholds(result: dict | None) -> tuple[list[str], list[str]]:
    """(met, missed) threshold names of an eval result."""
    if not result:
        return [], []
    met = sorted(k for k, ok in result["thresholds_met"].items() if ok)
    missed = sorted(k for k, ok in result["thresholds_met"].items() if not ok)
    return met, missed
