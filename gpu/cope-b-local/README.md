# CoPE-B on a Mac (spike #435, backend #437)

Runs the CoPE-B-A4B classifier locally on Apple Silicon with
[MLX](https://github.com/ml-explore/mlx-lm). Two ways to use it:

- **`server.py`** (#437) serves the RunPod worker's wire format on loopback,
  for Charcoal's `local` backend (`classify-gate`, `classify-compare`,
  `eval-classifiers`). Never a production classifier.
- **`classify_local.py`** (#435) is the standalone spike harness: classify a
  fixture file, fetch RunPod reference verdicts, compare.

Both run the same code in `engine.py`.

## Server

```
POLICY_VERSION=policy-ddac468-2026-07-03 \
  ~/.venvs/cope-b-mlx/bin/python gpu/cope-b-local/server.py --model ~/models/cope-b-a4b-mlx-8bit
```

- `POLICY_VERSION` is required, with no default: it must equal the deployed
  policy, the same value Charcoal's `CHARCOAL_COPE_B_POLICY_VERSION` holds.
  The policy text is read from `gpu/cope-b-runpod/policy.txt`.
- Binds `127.0.0.1:8765` (`--port` to change; `--host` accepts only
  `127.0.0.1` or `::1`). No auth.
- `POST /runsync` with `{"input": {"contents": [...]}}` returns a COMPLETED
  RunPod envelope; `GET /status/{id}` returns a recent one. One batch at a
  time.
- At startup it refuses to run if its prompt tokens differ from the
  transformers rendering production uses.

Tests (no model needed): `~/.venvs/cope-b-mlx/bin/python -m pytest gpu/cope-b-local/tests`

Docker or OrbStack will not work: a Linux VM on a Mac cannot use the GPU,
and vLLM needs a CUDA GPU. MLX runs natively on the Mac's GPU instead.

## Setup (one time)

```
python3 -m venv ~/.venvs/cope-b-mlx
~/.venvs/cope-b-mlx/bin/pip install mlx-lm 'huggingface_hub[cli]' pytest hypothesis
~/.venvs/cope-b-mlx/bin/hf download zentropi-ai/cope-b-a4b --local-dir ~/models/cope-b-a4b   # ~47 GB
~/.venvs/cope-b-mlx/bin/mlx_lm.convert --hf-path ~/models/cope-b-a4b \
    --mlx-path ~/models/cope-b-a4b-mlx-8bit -q --q-bits 8                                   # ~25 GB
```

## Run

```
P=~/.venvs/cope-b-mlx/bin/python
$P gpu/cope-b-local/classify_local.py local --model ~/models/cope-b-a4b-mlx-8bit --out /tmp/local.jsonl
$P gpu/cope-b-local/classify_local.py reference --out /tmp/ref.jsonl     # needs RUNPOD_* in env; one cold start
$P gpu/cope-b-local/classify_local.py compare --candidate /tmp/local.jsonl --reference /tmp/ref.jsonl
```

`compare` reads its bar from `src/cli/classify_gate.rs` (currently at
least 85% agreement on at least 50 rows), and scores every reference row: a
row the candidate is missing, or where either side errored, counts as a
disagreement.

## Measured on the M4 Pro Mac mini (64 GB), 2026-11-20

8-bit model, the 100-post `tests/fixtures/cope_b/ab_sample.jsonl`:

- **~5 classifications/s** with the policy prefix reused, 0.46/s without
- 27.8 GB peak memory, 4 s model load
- Reusing the prefix gives the same 100 labels as running every prompt in
  full (confidence differs by at most 0.089)

The policy is identical to the one in the deployed image
(`policy-ddac468-2026-07-03`).

## The sliding-window trap

Gemma 4 attends over only the last 1,024 tokens on most layers, so those
layers keep a rotating cache. The policy prefix is 1,174 tokens, so the
cache has already wrapped by the time a post starts. Rewinding (trimming) it
after each post cannot restore the overwritten prefix. The first version did
that and 70 of 100 answers came back as garbage tokens. The script instead
snapshots the cache once after the prefix and starts every post from a copy.

## Result vs production: 100/100 after turning thinking mode off

Against the production RunPod endpoint (vLLM, bf16, `policy-ddac468-2026-07-03`)
the 8-bit MLX model now agrees on **100/100** labels; confidence differs by a
median of 0.000 and at most 0.058.

### The thinking-mode trap

The first comparison was 97/100, and all 3 misses were production-toxic,
MLX-safe: two were anti-trans and anti-Pride replies, the hostility Charcoal
exists to catch. Full precision on MLX missed them too, while Hugging Face's
reference code (`classify_hf_cpu.py`, CPU) agreed with production, which
first looked like a bug in MLX's Gemma 4 maths.

It was not. A layer-by-layer comparison (`diff_layers.py`) stopped before the
first layer: the two sides were feeding the model **different tokens**.
mlx-lm's `apply_chat_template` turns thinking mode on by default for models
that support it, which inserts a `<|turn>system\n<|think|>` turn at the top.
transformers, and therefore production, leaves it off. With
`enable_thinking=False` the tokens are identical and so are the verdicts.

`classify_local.py` now renders the first row with plain transformers and
exits if MLX's tokens differ, so a template change cannot silently degrade
verdicts again. (Checked by flipping the flag back: the run stops with
"MLX prompt tokens differ".)
