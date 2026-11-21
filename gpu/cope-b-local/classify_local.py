"""Spike #435: run CoPE-B-A4B on a Mac (Apple Silicon, MLX) and compare its
verdicts with the production RunPod endpoint.

Three subcommands, each reading/writing JSONL so the steps can be rerun
independently:

  local      classify a fixture file with a local MLX model
  reference  classify the same file on the RunPod endpoint (full-precision
             vLLM, i.e. what production uses)
  compare    agreement between two verdict files, using the same 90% bar as
             `charcoal classify-gate`

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
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNPOD_DIR = HERE.parent / "cope-b-runpod"
sys.path.insert(0, str(RUNPOD_DIR))

from prompt import build_body  # noqa: E402  (path set up just above)

# Same bar as src/cli/classify_gate.rs (AGREEMENT_THRESHOLD / MIN_SAMPLE).
AGREEMENT_THRESHOLD = 0.90
MIN_SAMPLE = 50


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
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.models.cache import make_prompt_cache

    policy = Path(args.policy).read_text(encoding="utf-8")
    rows = read_rows(Path(args.sample))
    if args.limit:
        rows = rows[: args.limit]

    t0 = time.perf_counter()
    model, tokenizer = load(args.model)
    load_s = time.perf_counter() - t0
    print(f"loaded {args.model} in {load_s:.1f}s", file=sys.stderr)

    def prompt_tokens(content: str) -> list[int]:
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": build_body(policy=policy, content=content)}],
            tokenize=False,
            add_generation_prompt=True,
        )
        # The chat template already emits <bos>; don't add a second one.
        return tokenizer.encode(text, add_special_tokens=False)

    # The policy text (~900 tokens) is identical for every post, so we run it
    # through the model ONCE and reuse its key/value cache, the same idea as
    # vLLM's enable_prefix_caching in the RunPod worker. The shared prefix is
    # everything up to where two different contents first diverge.
    a, b = prompt_tokens("A"), prompt_tokens("B")
    prefix_len = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
    prefix = a[:prefix_len]

    # Snapshot-and-restore, NOT trim. Gemma 4 uses a 1,024-token sliding
    # window on most layers, and the policy prefix alone is longer than that,
    # so those layers keep a *rotating* cache. Rewinding ("trimming") a cache
    # that has already wrapped cannot bring back the prefix tokens the post
    # overwrote: in the first version of this script every post after the
    # first saw a corrupted policy and 70/100 answers were garbage tokens.
    # Instead we keep the post-prefix state and start each post from a copy.
    reuse = not args.no_prefix_cache
    snapshot = None
    if reuse:
        cache = make_prompt_cache(model)
        model(mx.array([prefix]), cache=cache)
        snapshot = [c.state for c in cache]
        mx.eval([x for st in snapshot for x in st if isinstance(x, mx.array)])

    def fresh_cache():
        cache = make_prompt_cache(model)
        if snapshot is not None:
            for layer, st in zip(cache, snapshot):
                # mx.array(x) makes a new array object, so the in-place cache
                # update during the post can't write through to the snapshot.
                layer.state = tuple(mx.array(x) if isinstance(x, mx.array) else x for x in st)
        return cache

    print(f"shared prefix {prefix_len} tokens; prefix cache {'ON' if reuse else 'OFF'}",
          file=sys.stderr)

    zero_id = tokenizer.encode("0", add_special_tokens=False)
    one_id = tokenizer.encode("1", add_special_tokens=False)
    if len(zero_id) != 1 or len(one_id) != 1:
        sys.exit(f"'0'/'1' are not single tokens: {zero_id} {one_id}")

    out = []
    t_start = time.perf_counter()
    for i, row in enumerate(rows):
        toks = prompt_tokens(row["content"])
        if toks[:prefix_len] != prefix:
            sys.exit(f"row {row['id']}: prompt does not start with the shared prefix")
        t = time.perf_counter()
        start = prefix_len if reuse else 0
        logits = model(mx.array([toks[start:]]), cache=fresh_cache())
        last = logits[0, -1, :].astype(mx.float32)
        logprobs = last - mx.logsumexp(last)
        top = int(mx.argmax(logprobs).item())
        token = tokenizer.decode([top]).strip().lstrip("▁")
        elapsed = time.perf_counter() - t

        if token not in {"0", "1"}:
            slot = {"ok": False, "error": f"unexpected model token: {token!r}"}
        else:
            slot = {
                "ok": True,
                "toxic": token == "1",
                "confidence": math.exp(float(logprobs[top].item())),
                # Kept for analysis: how sure the model was of "toxic",
                # whichever token it emitted.
                "p_toxic": math.exp(float(logprobs[one_id[0]].item())),
            }
        out.append({"id": row["id"], **slot, "tokens": len(toks), "seconds": round(elapsed, 3)})
        print(f"[{i + 1}/{len(rows)}] {row['id']} {slot.get('toxic')} "
              f"{slot.get('confidence', 0):.3f} {elapsed:.2f}s", file=sys.stderr)

    total = time.perf_counter() - t_start
    write_jsonl(Path(args.out), out)
    print(f"{len(out)} rows in {total:.1f}s = {len(out) / total:.2f} classifications/s "
          f"(model load {load_s:.1f}s excluded); peak memory "
          f"{mx.get_peak_memory() / 1e9:.1f} GB", file=sys.stderr)


# ------------------------------------------------------- reference (RunPod)


def _post(url: str, key: str, body: dict | None) -> dict:
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def cmd_reference(args) -> None:
    base = os.environ["RUNPOD_ENDPOINT_URL"].rstrip("/")
    key = os.environ["RUNPOD_API_KEY"]
    rows = read_rows(Path(args.sample))
    if args.limit:
        rows = rows[: args.limit]

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
    cand = {r["id"]: r for r in read_rows(Path(args.candidate))}
    ref = {r["id"]: r for r in read_rows(Path(args.reference))}
    ids = [i for i in ref if i in cand]
    usable = [i for i in ids if cand[i].get("ok") and ref[i].get("ok")]
    agree = [i for i in usable if cand[i]["toxic"] == ref[i]["toxic"]]
    errors = len(ids) - len(usable)
    rate = len(agree) / len(usable) if usable else 0.0
    passed = len(usable) >= MIN_SAMPLE and rate >= AGREEMENT_THRESHOLD

    print(f"rows compared: {len(usable)} (errors skipped: {errors})")
    print(f"agreement: {len(agree)}/{len(usable)} = {rate:.1%}  "
          f"-> {'PASS' if passed else 'FAIL'} (bar {AGREEMENT_THRESHOLD:.0%}, min {MIN_SAMPLE})")
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
