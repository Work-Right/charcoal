"""Spike #435: run CoPE-B-A4B on a Mac (Apple Silicon, MLX) and compare its
verdicts with the production RunPod endpoint.

Three subcommands, each reading/writing JSONL so the steps can be rerun
independently:

  local      classify a fixture file with a local MLX model
  reference  classify the same file on the RunPod endpoint (full-precision
             vLLM, i.e. what production uses)
  compare    agreement between two verdict files, using the bar read from
             src/cli/classify_gate.rs (the one `charcoal classify-gate` uses)

Why not just point `charcoal classify-gate` at a local server? The gate takes
two *backend names* and both of these are "runpod" with different URLs, so
there is no way to express "local CoPE-B vs RunPod CoPE-B" without changing
the Rust CLI. For a spike, a standalone script is cheaper and changes nothing
in the app.

The prompt is built by the SAME `build_body` the RunPod worker uses, and the
verdict/confidence math mirrors `handler.decode_verdict`: greedy single
token, confidence = exp(logprob of the emitted token) over the full
vocabulary. Any divergence must therefore come from the model weights
(quantization) or the runtime, which is exactly what this spike measures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNPOD_DIR = HERE.parent / "cope-b-runpod"

GATE_RS = HERE.parent.parent / "src/cli/classify_gate.rs"


def gate_bar() -> tuple[float, int]:
    """Read AGREEMENT_THRESHOLD and MIN_SAMPLE from the Rust gate itself.

    A copied constant drifted once already: the gate moved from 0.90 to 0.85
    (34bb90b) while this script still said 0.90. Reading the source keeps
    `compare` on the same bar as `charcoal classify-gate`, and fails loudly
    if the constants are renamed rather than guessing a value.
    """
    src = GATE_RS.read_text(encoding="utf-8")
    t = re.search(r"pub const AGREEMENT_THRESHOLD: f32 = ([0-9.]+);", src)
    n = re.search(r"pub const MIN_SAMPLE: usize = ([0-9]+);", src)
    if not (t and n):
        sys.exit(f"could not find AGREEMENT_THRESHOLD / MIN_SAMPLE in {GATE_RS}")
    return float(t.group(1)), int(n.group(1))


def read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# ---------------------------------------------------------------- local (MLX)


def cmd_local(args) -> None:
    # The classification logic (prompt, token guard, prefix snapshot) lives
    # in engine.py, shared with server.py, so the two can never drift.
    import mlx.core as mx

    from engine import load_engine

    rows = read_rows(Path(args.sample))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        sys.exit(f"no rows in {args.sample}")

    t0 = time.perf_counter()
    try:
        engine = load_engine(args.model, args.policy_version, policy_path=Path(args.policy),
                             reuse_prefix=not args.no_prefix_cache)
    except RuntimeError as exc:  # the engine's startup guards
        sys.exit(str(exc))
    load_s = time.perf_counter() - t0
    print(f"loaded {args.model} in {load_s:.1f}s; shared prefix {engine.prefix_len} tokens; "
          f"prefix cache {'OFF' if args.no_prefix_cache else 'ON'}", file=sys.stderr)

    out = []
    t_start = time.perf_counter()
    for i, row in enumerate(rows):
        t = time.perf_counter()
        slot = engine.classify_one(row["content"])
        elapsed = time.perf_counter() - t
        out.append({"id": row["id"], **slot, "seconds": round(elapsed, 3)})
        print(f"[{i + 1}/{len(rows)}] {row['id']} {slot.get('toxic')} "
              f"{slot.get('confidence', 0):.3f} {elapsed:.2f}s", file=sys.stderr)

    total = time.perf_counter() - t_start
    write_jsonl(Path(args.out), out)
    print(f"{len(out)} rows in {total:.1f}s = {len(out) / total:.2f} classifications/s "
          f"(model load {load_s:.1f}s excluded); {engine.uncached_posts} without the prefix "
          f"cache; peak memory {mx.get_peak_memory() / 1e9:.1f} GB", file=sys.stderr)


# ------------------------------------------------------- reference (RunPod)


def _post(url: str, key: str, body: dict | None) -> dict:
    # urllib also opens file:// and other schemes, so only ever talk HTTPS to
    # the endpoint (Semgrep dynamic-urllib-use). The URL comes from our own
    # RUNPOD_ENDPOINT_URL, but the check costs nothing.
    if not url.startswith("https://"):
        sys.exit(f"refusing non-HTTPS endpoint URL: {url!r}")
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        return json.load(resp)


def cmd_reference(args) -> None:
    base = os.environ["RUNPOD_ENDPOINT_URL"].rstrip("/")
    key = os.environ["RUNPOD_API_KEY"]
    rows = read_rows(Path(args.sample))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        sys.exit(f"no rows in {args.sample}; refusing to wake the endpoint for nothing")

    # One job for the whole sample: the worker batches it internally, and a
    # single job pays the cold start and idle tail once.
    job = _post(f"{base}/run", key, {"input": {"contents": [r["content"] for r in rows]}})
    job_id = job["id"]
    print(f"submitted job {job_id}", file=sys.stderr)
    deadline = time.time() + args.timeout
    while True:
        status = _post(f"{base}/status/{job_id}", key, None)
        if status["status"] == "COMPLETED":
            break
        if status["status"] in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            sys.exit(f"job {job_id} ended {status['status']}: {status}")
        if time.time() > deadline:
            sys.exit(f"job {job_id} still {status['status']} after {args.timeout}s")
        time.sleep(5)

    verdicts = status["output"]["verdicts"]
    if len(verdicts) != len(rows):
        sys.exit(f"got {len(verdicts)} verdicts for {len(rows)} rows")
    out = [{"id": r["id"], **v} for r, v in zip(rows, verdicts)]
    write_jsonl(Path(args.out), out)
    print(f"{len(out)} reference verdicts; delay {status.get('delayTime')} ms, "
          f"execution {status.get('executionTime')} ms; policy_version "
          f"{verdicts[0].get('policy_version')}", file=sys.stderr)


# ------------------------------------------------------------------ compare


def cmd_compare(args) -> None:
    threshold, min_sample = gate_bar()
    cand = {r["id"]: r for r in read_rows(Path(args.candidate))}
    ref = {r["id"]: r for r in read_rows(Path(args.reference))}
    # Score against EVERY reference row, like classify-gate. A row the
    # candidate is missing, or where either side errored, counts as a
    # disagreement: dropping it would let a run with many garbage answers
    # (the thinking-mode and cache-trim bugs both produced those) report
    # 100% on the few rows that survived.
    ids = list(ref)
    usable = [i for i in ids if i in cand and cand[i].get("ok") and ref[i].get("ok")]
    failed = [i for i in ids if i not in usable]
    agree = [i for i in usable if cand[i]["toxic"] == ref[i]["toxic"]]
    rate = len(agree) / len(ids) if ids else 0.0
    passed = len(ids) >= min_sample and rate >= threshold

    print(f"rows: {len(ids)} (missing or errored, counted as disagreeing: {len(failed)})")
    print(f"agreement: {len(agree)}/{len(ids)} = {rate:.1%}  "
          f"-> {'PASS' if passed else 'FAIL'} (bar {threshold:.0%}, min {min_sample})")
    for i in failed:
        why = "missing from candidate" if i not in cand else (
            cand[i].get("error") if not cand[i].get("ok") else ref[i].get("error"))
        print(f"  FAILED ROW {i}: {why}")
    stricter_local = [i for i in usable if cand[i]["toxic"] and not ref[i]["toxic"]]
    stricter_ref = [i for i in usable if ref[i]["toxic"] and not cand[i]["toxic"]]
    print(f"local says toxic, reference safe: {len(stricter_local)}")
    print(f"reference says toxic, local safe: {len(stricter_ref)}")
    # Confidence drift on rows that agree: a quantized model can keep the
    # same label while getting less sure, which matters near the 0.5 line.
    drift = [abs(cand[i]["confidence"] - ref[i]["confidence"]) for i in agree]
    if drift:
        drift.sort()
        print(f"confidence drift on agreeing rows: median {drift[len(drift) // 2]:.3f}, "
              f"max {drift[-1]:.3f}")
    for i in stricter_local + stricter_ref:
        print(f"  DISAGREE {i}: local {cand[i]['toxic']} ({cand[i]['confidence']:.2f}) "
              f"vs reference {ref[i]['toxic']} ({ref[i]['confidence']:.2f})")
    sys.exit(0 if passed else 1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    default_sample = str(HERE.parent.parent / "tests/fixtures/cope_b/ab_sample.jsonl")

    lo = sub.add_parser("local")
    lo.add_argument("--model", required=True, help="MLX model dir or HF repo")
    lo.add_argument("--policy", default=str(RUNPOD_DIR / "policy.txt"))
    lo.add_argument("--sample", default=default_sample)
    lo.add_argument("--out", required=True)
    lo.add_argument("--limit", type=int, default=0)
    lo.add_argument("--no-prefix-cache", action="store_true")
    # Only labels the output slots; the local run makes no policy check.
    lo.add_argument("--policy-version", default="local-unchecked")
    lo.set_defaults(func=cmd_local)

    re_ = sub.add_parser("reference")
    re_.add_argument("--sample", default=default_sample)
    re_.add_argument("--out", required=True)
    re_.add_argument("--limit", type=int, default=0)
    re_.add_argument("--timeout", type=int, default=900)
    re_.set_defaults(func=cmd_reference)

    co = sub.add_parser("compare")
    co.add_argument("--candidate", required=True)
    co.add_argument("--reference", required=True)
    co.set_defaults(func=cmd_compare)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
