"""Tiebreaker for spike #435: classify a fixture file with Hugging Face's own
transformers implementation of Gemma 4, in bf16 on the CPU.

When MLX (on the Mac) and vLLM (on RunPod) disagree, this is the official
reference implementation of the architecture, so whichever of the two it
matches is the one computing the model correctly. It is slow (no GPU), so
use it on a handful of rows, not a full sample.

Usage:
  python classify_hf_cpu.py --model ~/models/cope-b-a4b --sample rows.jsonl --out hf.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cope-b-runpod"))

from prompt import build_body  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--sample", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--policy", default=str(HERE.parent / "cope-b-runpod" / "policy.txt"))
    args = p.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    policy = Path(args.policy).read_text(encoding="utf-8")
    tok = AutoTokenizer.from_pretrained(args.model)
    t0 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16)
    model.eval()
    print(f"loaded in {time.perf_counter() - t0:.0f}s", file=sys.stderr)

    one_id = tok.encode("1", add_special_tokens=False)[0]
    out = []
    for line in Path(args.sample).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        text = tok.apply_chat_template(
            [{"role": "user", "content": build_body(policy=policy, content=row["content"])}],
            tokenize=False,
            add_generation_prompt=True,
        )
        # Same tokens as the MLX harness: the template already has <bos>.
        ids = tok(text, add_special_tokens=False, return_tensors="pt").input_ids
        t = time.perf_counter()
        with torch.no_grad():
            logits = model(ids).logits[0, -1].float()
        logprobs = torch.log_softmax(logits, dim=-1)
        top = int(torch.argmax(logprobs))
        token = tok.decode([top]).strip()
        slot = (
            {"ok": True, "toxic": token == "1", "confidence": math.exp(float(logprobs[top])),
             "p_toxic": math.exp(float(logprobs[one_id]))}
            if token in {"0", "1"}
            else {"ok": False, "error": f"unexpected model token: {token!r}"}
        )
        out.append({"id": row["id"], **slot})
        print(f"{row['id']} {slot.get('toxic')} {slot.get('confidence', 0):.3f} "
              f"{time.perf_counter() - t:.0f}s", file=sys.stderr)

    Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in out), encoding="utf-8")


if __name__ == "__main__":
    main()
