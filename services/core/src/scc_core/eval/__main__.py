"""Eval runner CLI (P2.02, P2.03): `uv run python -m scc_core.eval run ...`. See docs/EVALS.md.

Two modes:
- live: calls the dev gateway, spends from the eval budget lane (D-021), refreshes the recording of the
  tuning split, writes a run file to evals/runs/ and a row to docs/EVALS.md;
- replay: answers from the recording, free and offline. CI runs this on every pull request (D-043).

The held-out split runs live only, is never recorded and needs a reason: it is for release candidates.
"""

import argparse
import asyncio
import copy
import datetime
import json
import os
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

from scc_core import config as core_config
from scc_core.eval import cases as eval_cases
from scc_core.eval import report
from scc_core.eval.metrics import aggregate, compare, summarize
from scc_core.eval.replay import EvalTransport
from scc_core.eval.runner import RunAborted, run_cases, scratch_database

REPO = Path(__file__).resolve().parents[5]


def _commit() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, cwd=REPO,
        ).stdout.strip()  # fmt: skip
        dirty = subprocess.run(
            # Files the runner itself writes don't make the code dirty.
            ["git", "status", "--porcelain", "--untracked-files=no", "--", ".",
             ":!docs/EVALS.md", ":!evals"],
            capture_output=True, text=True, check=True, cwd=REPO,
        ).stdout.strip()  # fmt: skip
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return sha + ("+dirty" if dirty else "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scc_core.eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the eval set through the pipeline")
    run.add_argument("--mode", choices=["live", "replay"], default="replay")
    run.add_argument("--split", choices=list(eval_cases.SPLITS), default="tuning")
    run.add_argument(
        "--config",
        default=None,
        help="pipeline config version (default: the active one)",
    )
    run.add_argument("--evals", type=Path, default=REPO / "evals")
    run.add_argument(
        "--gateway", default="http://127.0.0.1:18082", help="live mode: the dev gateway"
    )
    run.add_argument(
        "--admin-dsn",
        default=os.environ.get("SCC_EVAL_ADMIN_DSN")
        or os.environ.get("SCC_TEST_ADMIN_DSN"),
        help="PostgreSQL superuser DSN for the throwaway database (or SCC_EVAL_ADMIN_DSN)",
    )
    run.add_argument(
        "--cases",
        default="",
        help="comma-separated case IDs: a partial run, never saved",
    )
    run.add_argument("--notes", default="", help="a note for the EVALS.md row")
    run.add_argument(
        "--held-out-reason", default="", help="required for --split held_out"
    )
    run.add_argument(
        "--write-baseline", action="store_true", help="make this run the new baseline"
    )
    run.add_argument(
        "--summary", type=Path, help="append the Markdown summary to this file"
    )
    run.add_argument(
        "--out",
        type=Path,
        help="also write the full run file here (any run, even partial)",
    )
    run.add_argument(
        "--check", action="store_true", help="exit 1 on stale cases or new failures"
    )
    run.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="live mode: run the split this many times; reports means and verdict stability",
    )
    run.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="experiment override of one config value, e.g. search.articles_to_assess=3 (TOML value)",
    )
    return parser


def apply_overrides(config: dict, overrides: list[str]) -> dict:
    """A copy of the config with each `dotted.path=value` set; the value is parsed as TOML."""
    config = copy.deepcopy(config)
    for item in overrides:
        path, _, raw = item.partition("=")
        if not raw:
            raise ValueError(f"--set needs PATH=VALUE, got {item!r}")
        *parents, leaf = path.strip().split(".")
        node = config
        for key in parents:
            child = node.get(key)
            if not hasattr(
                child, "items"
            ):  # a missing key, or a value where a section was expected
                raise ValueError(f"--set: no config section {key!r} in {path!r}")
            node = child
        node[leaf] = tomllib.loads(f"v = {raw}")["v"]
    return config


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The summary has ≥ and ✅; Windows consoles default to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    if not args.admin_dsn:
        print(
            "Needs a PostgreSQL superuser DSN: --admin-dsn or SCC_EVAL_ADMIN_DSN.",
            file=sys.stderr,
        )
        return 2
    held_out = args.split == "held_out"
    if held_out and args.mode == "replay":
        print(
            "The held-out split is never recorded, so it can't be replayed.",
            file=sys.stderr,
        )
        return 2
    if args.repeat < 1 or (args.repeat > 1 and args.mode != "live"):
        print(
            "--repeat needs live mode: a replay repeats the same answers.",
            file=sys.stderr,
        )
        return 2
    if (args.repeat > 1 or args.set) and args.write_baseline:
        print(
            "A baseline comes from one plain run of a config version.", file=sys.stderr
        )
        return 2
    if held_out and not args.held_out_reason:
        print(
            "Held-out runs are for release candidates: give --held-out-reason.",
            file=sys.stderr,
        )
        return 2

    evalset = eval_cases.load(args.evals, args.split)
    selected = [
        c for c in evalset.cases if not args.cases or c.id in args.cases.split(",")
    ]
    partial = len(selected) != len(evalset.cases)
    config = apply_overrides(core_config.load(args.config), args.set)
    store = args.evals / "recordings" / "tuning"
    # A recording belongs to one real config version: never from repeats or overrides.
    record = (
        args.mode == "live"
        and not held_out
        and not partial
        and args.repeat == 1
        and not args.set
    )
    transport = EvalTransport(
        args.mode, store if (record or args.mode == "replay") else None
    )

    now = datetime.datetime.now(datetime.UTC)
    run_id = f"{now:%Y%m%d-%H%M%S}-{args.mode}-{secrets.token_hex(2)}"
    meta = {
        "run_id": run_id,
        "date": f"{now:%Y-%m-%d}",
        "mode": args.mode,
        "split": args.split,
        "evalset": evalset.version,
        "config_version": config["version"] + ("+set" if args.set else ""),
        "overrides": args.set,
        "repeats": args.repeat,
        "model": config["llm"]["model"],
        "commit": _commit(),
        "thresholds": evalset.thresholds,
        "partial": partial,
        "notes": "; ".join(
            filter(
                None,
                [
                    args.held_out_reason or args.notes,
                    f"mean of {args.repeat} runs" if args.repeat > 1 else "",
                    *args.set,
                ],
            )
        ),
    }
    print(
        f"Eval {run_id}: {len(selected)} {args.split} case(s), {meta['config_version']}, {args.mode}"
    )

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with scratch_database(args.admin_dsn) as conninfo:
            repeats = []
            for i in range(args.repeat):
                if args.repeat > 1:
                    print(f" run {i + 1} of {args.repeat}")
                repeats.append(
                    asyncio.run(
                        run_cases(
                            selected,
                            config,
                            transport,
                            conninfo,
                            f"{run_id}-r{i + 1}" if args.repeat > 1 else run_id,
                            args.gateway,
                        ),
                        loop_factory=loop_factory,
                    )
                )
    except RunAborted as exc:
        print(f"Run aborted: {exc}", file=sys.stderr)
        return 2

    if args.repeat > 1:
        summary, results = aggregate(repeats, evalset.thresholds)
    else:
        results = repeats[0]
        summary = summarize(results, evalset.thresholds)
    baseline_path = args.evals / f"baseline-{args.split}.json"
    comparison = None
    if baseline_path.exists() and args.repeat == 1 and not args.set:
        baseline = json.loads(baseline_path.read_text("utf-8"))
        if baseline["evalset"] == evalset.version:
            comparison = compare(results, baseline)
    text = report.markdown(meta, summary, comparison, results)
    print()
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(text)

    if args.out:
        report.write_json(
            args.out, report.run_document(meta, summary, comparison, results, repeats)
        )
    if args.mode == "live" and not partial:
        report.write_json(
            args.evals / "runs" / f"{run_id}.json",
            report.run_document(meta, summary, comparison, results, repeats),
        )
        report.append_evals_md(
            REPO / "docs" / "EVALS.md", report.evals_md_row(meta, summary)
        )
        if record:
            transport.save()
        print(f"Saved evals/runs/{run_id}.json and a row in docs/EVALS.md.")
    if args.write_baseline and not partial:
        report.write_json(baseline_path, report.baseline_document(meta, results))
        print(f"Wrote {baseline_path.name}.")

    if args.check and (
        summary["stale"] or (comparison and comparison["newly_failing"])
    ):
        print(
            "Eval check failed: stale recordings or cases that passed in the baseline now fail."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
