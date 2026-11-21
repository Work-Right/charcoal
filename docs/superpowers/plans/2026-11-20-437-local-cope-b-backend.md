# #437 Local CoPE-B backend: implementation plan

Spec: `docs/superpowers/specs/2026-11-20-437-local-cope-b-backend-design.md`.
Each task writes its tests first (they must fail), then the code. Every
task ends with its tests green and a commit.

## Task 1: Python server, wire format (D3, D8)

Files: `gpu/cope-b-local/server.py`, `gpu/cope-b-local/tests/test_server.py`.

1. Factor the classify loop out of `classify_local.py` into a
   `Classifier` class (load model, token guard, prefix snapshot,
   `classify(contents) -> list[slot]`). `classify_local.py local` keeps
   working through it.
2. Tests first, with a fake `Classifier`:
   - Property (Hypothesis): for any batch and any set of posts that raise,
     the response has one slot per input in order, and only the raising
     positions are `ok: false`.
   - `POST /runsync` returns `status: COMPLETED`; `GET /status/{id}`
     returns the same envelope; a missing `contents` key returns HTTP 400
     (the RunPod worker raises `KeyError`, so match that contract in the
     client test of Task 4).
3. Implement with `http.server.ThreadingHTTPServer` and one lock around
   `classify`.

## Task 2: Python server, startup refusals (D5, D6, D7)

Tests first:
- no `POLICY_VERSION` → exits non-zero with a message naming the variable
- `--host` other than `127.0.0.1` or `::1` → exits
- a fake tokenizer pair whose renderings differ → exits with the token
  mismatch message
- prefix snapshot: with a fake cache, classifying A then B leaves B's
  result identical to classifying B alone (guards the trim bug)

Then implement. `policy.txt` is read from `gpu/cope-b-runpod/` (spec open
question 2).

## Task 3: Rust client identity (D4)

Files: `src/toxicity/runpod_cope_b.rs`.

1. Tests first, each comment stating the requirement:
   - `RunPodCopeBClient::local(url)` reports `name() == "local-cope-b"` and
     `model_id() == "cope-b-a4b-mlx-8bit"`, so cached local verdicts cannot
     be read as production ones.
   - the production constructor still reports `runpod-cope-b` /
     `cope-b-a4b` (guards against the identity leaking the other way).
2. Implement: store `name`/`model_id` as `&'static str` fields set by the
   constructor; `local` passes a fixed placeholder API key and a disabled
   meter.
3. `cargo mutants --in-diff` on the change.

## Task 4: backend selection (D2, D6)

Files: `src/toxicity/classifier.rs`, `tests/` (wiremock).

1. Tests first:
   - `build_backend_named("local")` reads `CHARCOAL_LOCAL_COPE_B_URL`,
     defaulting to `http://127.0.0.1:8765`.
   - `CHARCOAL_CLASSIFIER=local` is rejected by `build_from_env*` (D2:
     production must never select it).
   - wiremock: the `local` backend round-trips the D3 envelope, and a slot
     with the wrong `policy_version` is refused, same as RunPod.
2. Implement; update the `unknown backend` message to list `local`.
3. Add fuzz seeds for local-server responses to
   `fuzz/seeds/runpod_batch_response/`; run `scripts/fuzz.sh
   runpod_batch_response 120`.
4. Mutation-test the diff.

## Task 5: eval-classifiers arm

Files: `src/cli/eval_classifiers/models.rs`.

1. Test first: `build_eval_model("local-cope-b", cap)` requires the policy
   version like `cope-b` does, and builds a client with the local identity.
2. Implement (no cost meter: local is free).

## Task 6: acceptance (model-gated, by hand)

```
POLICY_VERSION=policy-ddac468-2026-07-03 \
  ~/.venvs/cope-b-mlx/bin/python gpu/cope-b-local/server.py --model ~/models/cope-b-a4b-mlx-8bit &
CHARCOAL_COPE_B_POLICY_VERSION=policy-ddac468-2026-07-03 \
  cargo run -- classify-gate --candidate local --reference runpod \
  --sample tests/fixtures/cope_b/ab_sample.jsonl
```

Must PASS (spike result: 100/100). Paste the output into the PR. Wakes
RunPod once (~$0.30) for the reference side.

## Docs

- `gpu/cope-b-local/README.md`: server usage.
- `README.md`: one paragraph under the classifier section.
- `CHANGELOG.md`: handwritten entry (never `chainlink close`'s generated
  line).

## Handoff

Nothing here is started. Setup on the Mac mini is ready (venv and 8-bit
model, see the spec). Start with Task 1; Tasks 3 to 5 do not depend on the
Python tasks and can run in parallel with them.
