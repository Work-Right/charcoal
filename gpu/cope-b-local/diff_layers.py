"""Spike #435: find where MLX's Gemma 4 diverges from Hugging Face's.

Both bf16 models don't fit in 64 GB at once, so this runs in two passes:

  hf    run the transformers reference on CPU and save, for each prompt, the
        hidden state after every decoder layer (all positions) to an .npz
  mlx   run mlx-lm on the same prompts, capture the same per-layer states,
        and print how far each layer is from the reference

Prompts: one short (well under the 1,024-token sliding window) and the real
classifier prompt for a post MLX gets wrong (longer than the window). If the
short one matches and the long one doesn't, the sliding-window path is the
suspect.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cope-b-runpod"))
from prompt import build_body  # noqa: E402

SAMPLE = HERE.parent.parent / "tests/fixtures/cope_b/ab_sample.jsonl"
MISS_ID = "cand-bafyreih-9"


def prompts(tokenizer) -> dict[str, list[int]]:
    policy = (HERE.parent / "cope-b-runpod" / "policy.txt").read_text(encoding="utf-8")
    row = next(r for r in map(json.loads, SAMPLE.read_text(encoding="utf-8").splitlines())
               if r["id"] == MISS_ID)

    def ids(body: str) -> list[int]:
        text = tokenizer.apply_chat_template([{"role": "user", "content": body}],
                                             tokenize=False, add_generation_prompt=True,
                                             enable_thinking=False)
        return tokenizer.encode(text, add_special_tokens=False)

    return {
        "short": ids("Is this post hostile? Answer 1 or 0.\n\n" + row["content"]),
        "long": ids(build_body(policy=policy, content=row["content"])),
    }


def cmd_hf(args) -> None:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16).eval()
    arrays = {}
    for name, ids in prompts(tok).items():
        with torch.no_grad():
            out = model(torch.tensor([ids]), output_hidden_states=True)
        # hidden_states[0] = embeddings, [i] = after layer i-1. The last one
        # has the final norm applied in HF, so keep it separately.
        for i, h in enumerate(out.hidden_states):
            arrays[f"{name}/h{i}"] = h[0].float().numpy()
        arrays[f"{name}/logits_last"] = out.logits[0, -1].float().numpy()
        arrays[f"{name}/ids"] = np.array(ids)
        print(f"{name}: {len(ids)} tokens, {len(out.hidden_states)} hidden states", file=sys.stderr)
    np.savez(args.out, **arrays)


def cmd_mlx(args) -> None:
    import mlx.core as mx
    from mlx_lm import load

    ref = np.load(args.ref)
    model, tok = load(args.model)
    inner = model.model if hasattr(model, "model") else model
    layers = inner.layers

    # Wrap each decoder layer so calling the model records its output.
    captured: list = []

    class Tap:
        def __init__(self, layer):
            self.layer = layer

        def __call__(self, *a, **k):
            out = self.layer(*a, **k)
            captured.append(out[0] if isinstance(out, tuple) else out)
            return out

        def __getattr__(self, name):
            return getattr(self.layer, name)

    inner.layers = [Tap(l) for l in layers]

    for name, ids in prompts(tok).items():
        assert list(ref[f"{name}/ids"]) == ids, f"{name}: token ids differ between HF and MLX"
        captured.clear()
        logits = model(mx.array([ids]))
        mx.eval(logits, captured)
        n = len(captured)
        print(f"\n== {name}: {len(ids)} tokens, {n} layers ==")
        print(" layer   rel.err(all pos)  rel.err(last tok)  worst position")
        for i, h in enumerate(captured):
            got = np.array(h[0].astype(mx.float32))
            # HF's final hidden state has the final norm applied; skip it.
            if i + 1 == n:
                break
            want = ref[f"{name}/h{i + 1}"]
            err = np.linalg.norm(got - want, axis=-1) / (np.linalg.norm(want, axis=-1) + 1e-6)
            print(f" {i:5d}   {err.mean():14.4f}   {err[-1]:15.4f}   {int(err.argmax()):6d}"
                  f"  ({inner.layers[i].layer.__class__.__name__}"
                  f"{', sliding' if getattr(inner.layers[i].layer, 'is_sliding', None) else ''})")
        got_l = np.array(logits[0, -1].astype(mx.float32))
        want_l = ref[f"{name}/logits_last"]
        print(f" final logits rel.err {np.linalg.norm(got_l - want_l) / np.linalg.norm(want_l):.4f};"
              f" argmax MLX {tok.decode([int(got_l.argmax())])!r} vs HF {tok.decode([int(want_l.argmax())])!r}")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("hf")
    a.add_argument("--model", required=True)
    a.add_argument("--out", required=True)
    a.set_defaults(func=cmd_hf)
    b = sub.add_parser("mlx")
    b.add_argument("--model", required=True)
    b.add_argument("--ref", required=True)
    b.set_defaults(func=cmd_mlx)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
