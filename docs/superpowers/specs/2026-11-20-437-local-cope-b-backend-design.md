# #437 Local CoPE-B backend (Mac mini, MLX): design

Status: DRAFT for Bryan's review. Written 2026-11-20, after spike #435.
Not scheduled: RunPod stays the production classifier. This document and the
plan below are the groundwork so the work can start whenever it is wanted.

## Why

Spike #435 showed CoPE-B-A4B runs on the M4 Pro Mac mini through MLX and
gives the same verdicts as production:

- 8-bit MLX model vs the RunPod endpoint (`policy-ddac468-2026-07-03`) on
  `tests/fixtures/cope_b/ab_sample.jsonl`: **100/100 labels agree**, median
  confidence difference 0.000, max 0.058
- ~5 classifications/s, 27.8 GB peak memory, 4 s model load, no cold start
- Tooling: `gpu/cope-b-local/` (README has the full measurements)

Every classifier eval, `classify-gate` and `classify-compare` run currently
wakes a RunPod H100 (2 to 4 min cold start, about $0.30 for 100 posts). A
local backend makes those runs free and immediate.

## Scope

In: a `local` backend for the developer tools that compare or evaluate
classifiers. `classify-gate` and `classify-compare` pick backends through
`build_backend_named` (`src/main.rs:1123`, `:1249`), so they get it for free.
`eval-classifiers` builds its models separately in `build_eval_model`
(`src/cli/eval_classifiers/models.rs`) because it needs the cost meter, so it
needs its own `local-cope-b` arm (free, so no meter; the policy-version
requirement still applies).

Out:
- **Production.** The web app on Railway must never depend on the Mac being
  on. See D2 for how that is enforced.
- **`charcoal estimate`.** It deliberately classifies nothing (it counts the
  posts a scan would send, `src/toxicity/estimate_only.rs`). A "full local
  scan" with real verdicts would be a separate feature.
- CoCore or any other remote host (#435 discussion: hosts need 32 GB+ Macs,
  and logprobs and thinking-mode control through their API are unverified).

## Decisions

Each one is a call made so the work is unblocked. Bryan can override any.

**D1. The model runs in a small Python server, not inside the Rust binary.**
MLX has no mature Rust binding, and the spike code (prompt building, token
guard, cache snapshot) is already Python and proven. The server lives in
`gpu/cope-b-local/server.py`, uses only the standard library plus mlx-lm and
transformers, and loads the model once.

**D2. `local` is NOT selectable through `CHARCOAL_CLASSIFIER`.** Only
`build_backend_named("local")` builds it, which is what the gate, compare and
eval tools call. This copies the precedent of `EstimateOnlyClassifier`: a
deployment that picked it by mistake would fail every scan. A test asserts
`CHARCOAL_CLASSIFIER=local` is rejected.

**D3. The server speaks the RunPod worker's wire format,** so the existing
`RunPodCopeBClient` (retries, batching, job-envelope parsing, policy-version
check) is reused instead of duplicated:

- `POST /runsync` with `{"input": {"contents": ["…", …]}}` returns
  `{"id": "…", "status": "COMPLETED", "output": {"verdicts": [slot, …]}}`
- each slot is `{ok, toxic, confidence, model, policy_version}` or
  `{ok: false, error}`, positionally aligned, same as `handler.py`
- `GET /status/{id}` returns the same envelope (the client polls it if
  `/runsync` ever reports a pending job; the server never does)

**D4. Local verdicts must never mix with production verdicts.** The
`classifier_verdicts` cache is keyed by `(text_sha256, model_id,
policy_version)`, and `CachedClassifier` takes `model_id` from the wrapped
client's `ToxicityClassifier::model_id()`, not from the server's reply.
`RunPodCopeBClient` hard-codes `name() = "runpod-cope-b"` and
`model_id() = "cope-b-a4b"` (`src/toxicity/runpod_cope_b.rs:695`). So the
`local` backend needs its own identity: add a constructor option (e.g.
`RunPodCopeBClient::local(url)`) that reports `name() = "local-cope-b"` and
`model_id() = "cope-b-a4b-mlx-8bit"`. The server also reports that `model`
string in every slot. A test pins both, so a local verdict can never be
served from cache as a production one, or the reverse.

**D5. Policy version is required, with no default.** The server refuses to
start without `POLICY_VERSION` set, and reports it in every slot. Two checks
keep the label honest (both added during review of PR #173):

- the server refuses a deployed-shaped label (`policy-<sha>-<date>`) unless
  the loaded `policy.txt` is byte-identical to that file at `<sha>`; drafts
  use a `draft-…` label;
- the client does NOT refuse a mismatched batch: it only logs it, and the
  verdicts still come back (#344 V3-01). So `classify-gate` and
  `classify-compare` now run each backend's identity probe before
  classifying, as the web scan paths and `eval-classifiers` already did.
  An earlier draft of this spec wrongly said the client refuses.

This follows the project's no-silent-fallback rule.

**D6. Loopback only, no auth.** The server binds `127.0.0.1` and refuses any
other bind address. `RunPodCopeBClient::new` requires a non-empty API key, so
the `local` backend passes a fixed placeholder; the server ignores the header.
The URL comes from `CHARCOAL_LOCAL_COPE_B_URL`, default
`http://127.0.0.1:8765`.

**D7. Correctness guards from the spike are mandatory and tested:**
- `enable_thinking=False` when building the prompt. mlx-lm turns thinking
  mode on by default; that silently missed anti-trans and anti-Pride posts
  production flags (#435).
- At startup, render one prompt with plain transformers and exit if MLX's
  tokens differ.
- Reuse the policy prefix by snapshotting the KV cache and restoring a copy
  per post, never by trimming (Gemma 4's 1,024-token sliding window is
  shorter than the 1,174-token prefix; trimming corrupted 70/100 answers).

**D8. One request at a time.** The server classifies posts sequentially
behind a lock. MLX on one Mac gains little from concurrency, and sequential
keeps the cache-snapshot logic simple. A batch of N posts is one request.

## Testing (per CLAUDE.md testing rules)

Python, `gpu/cope-b-local/tests/`, MLX mocked like
`gpu/cope-b-runpod/tests/` so they run without the model:

- Wire format: each response slot is aligned with its input, and an
  exception on one post yields an `ok: false` slot for that post only
  (property test with Hypothesis over random batches and failure positions).
- Startup refuses: missing `POLICY_VERSION`, non-loopback bind, token
  mismatch between MLX and transformers.
- Prefix snapshot: the cache state after classifying post A then B equals
  the state from classifying B alone (fake cache object).

Rust:

- `build_backend_named("local")` builds a client named `local` against
  `CHARCOAL_LOCAL_COPE_B_URL`, and `CHARCOAL_CLASSIFIER=local` is rejected
  (requirement D2 stated in each assertion's comment).
- wiremock integration test: the `local` backend round-trips the D3 wire
  format and fails on a policy-version mismatch.
- `parse_batch_response` already has a fuzz target
  (`runpod_batch_response`); add seeds for the local server's responses.
- Mutation-test the diff: `cargo mutants --in-diff`.

Model-gated (real weights, run by hand, never silently skipped):

- `classify-gate --candidate local --reference runpod` on `ab_sample.jsonl`
  must pass (expect 100/100). This is the acceptance test.

## Setup this depends on

Already in place on the Mac mini (2026-11-20):

- venv `~/.venvs/cope-b-mlx` (mlx-lm 0.32.2, transformers 5.23)
- 8-bit model `~/models/cope-b-a4b-mlx-8bit` (25 GB, includes tokenizer and
  chat template). The 47 GB original was deleted; re-download only to
  re-convert.

The README in `gpu/cope-b-local/` has the commands to recreate both.

## Open questions

1. Should a model-gated Python test run in CI? There is no Apple Silicon
   runner, so the acceptance gate stays a manual step. Recommendation: manual,
   recorded in the PR.
2. When the deployed policy changes, the local server needs the new
   `policy.txt` and `POLICY_VERSION`. Recommendation: read `policy.txt` from
   `gpu/cope-b-runpod/` (one source of truth) and require the version on the
   command line.
