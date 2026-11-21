"""CoPE-B classification on Apple Silicon (#437).

Two layers, so the logic can be tested without a 25 GB model:

* `CopeBEngine` holds the classification logic: reuse the policy prefix,
  start every post from a clean copy of the cache, and turn the model's next
  token into a verdict slot. It talks only to a small backend interface.
* `MlxBackend` implements that interface with mlx-lm.

Both lessons from spike #435 live here:

* Thinking mode must be OFF. mlx-lm turns it on by default for Gemma 4, which
  adds a `<|think|>` turn production never sends; with it on, the model
  missed anti-trans and anti-Pride posts production flags. `MlxBackend`
  passes `enable_thinking=False` and refuses to start if its prompt tokens
  differ from the transformers rendering production uses.
* Never trim the cache. Gemma 4 attends over only the last 1,024 tokens on
  most layers and the policy prefix is longer, so a trimmed cache has
  already lost part of the prefix. Snapshot it once, restore a copy per post.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Callable, Protocol, Sequence

HERE = Path(__file__).resolve().parent
RUNPOD_DIR = HERE.parent / "cope-b-runpod"
sys.path.insert(0, str(RUNPOD_DIR))

from prompt import build_body  # noqa: E402  (path set up just above)

# What the local server reports as `model`, and what the Rust `local`
# backend reports as its model_id. Distinct from production's "cope-b-a4b"
# so local verdicts never share cache rows with production ones (#437 D4).
LOCAL_MODEL_NAME = "cope-b-a4b-mlx-8bit"
DEFAULT_POLICY_PATH = RUNPOD_DIR / "policy.txt"


class Backend(Protocol):
    zero_id: int
    one_id: int

    def prompt_tokens(self, content: str) -> list[int]: ...
    def new_cache(self): ...
    def forward(self, tokens: list[int], cache) -> Sequence[float]: ...
    def snapshot(self, cache): ...
    def restore(self, snap): ...
    def decode(self, token_id: int) -> str: ...


def shared_prefix_len(a: Sequence[int], b: Sequence[int]) -> int:
    """Length of the longest common prefix of two token lists."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def slot_from_logprobs(logprobs: Sequence[float], decode: Callable[[int], str],
                       one_id: int, model: str, policy_version: str) -> dict:
    """Greedy next token -> verdict slot, the same contract as the RunPod
    worker's `handler.result_slot`: "1" toxic, "0" safe, confidence =
    exp(logprob of the emitted token) over the full vocabulary."""
    top = max(range(len(logprobs)), key=lambda i: logprobs[i])
    token = decode(top).strip().lstrip("▁")
    if token not in {"0", "1"}:
        return {"ok": False, "error": f"unexpected model token: {token!r}"}
    return {
        "ok": True,
        "toxic": token == "1",
        "confidence": math.exp(float(logprobs[top])),
        "model": model,
        "policy_version": policy_version,
        # Not part of the wire contract (the client ignores unknown fields);
        # kept for analysis: how likely "toxic" was, whichever token won.
        "p_toxic": math.exp(float(logprobs[one_id])),
    }


class CopeBEngine:
    def __init__(self, backend: Backend, *, model_name: str, policy_version: str,
                 reuse_prefix: bool = True):
        self.backend = backend
        self.model_name = model_name
        self.policy_version = policy_version
        # How many posts had to skip the prefix cache (see classify_one).
        self.uncached_posts = 0

        # The shared prefix is everything up to where two different posts'
        # prompts first diverge: the chat template and the whole policy.
        a, b = backend.prompt_tokens("A"), backend.prompt_tokens("B")
        self.prefix_len = shared_prefix_len(a, b)
        self.prefix = a[: self.prefix_len]
        self._snapshot = None
        if reuse_prefix:
            cache = backend.new_cache()
            backend.forward(self.prefix, cache)
            self._snapshot = backend.snapshot(cache)

    def classify_one(self, content: str) -> dict:
        toks = self.backend.prompt_tokens(content)
        # A post that starts with whitespace can merge with the blank line
        # before CONTENT into a different token, so its prompt does not start
        # with the shared prefix. Run that post in full: slower, still right.
        # len > prefix_len: the continuation fed to the model must be non-empty.
        cached = (self._snapshot is not None and len(toks) > self.prefix_len
                  and toks[: self.prefix_len] == self.prefix)
        if cached:
            logprobs = self.backend.forward(toks[self.prefix_len:],
                                            self.backend.restore(self._snapshot))
        else:
            if self._snapshot is not None:
                self.uncached_posts += 1
            logprobs = self.backend.forward(toks, self.backend.new_cache())
        return slot_from_logprobs(logprobs, self.backend.decode, self.backend.one_id,
                                  self.model_name, self.policy_version)


class MlxBackend:
    """mlx-lm implementation of `Backend`. Loading takes a few seconds and
    about 28 GB of unified memory for the 8-bit model."""

    def __init__(self, model_path: str, policy: str):
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache
        from transformers import AutoTokenizer

        self._mx = mx
        self._make_cache = make_prompt_cache
        self.model, self.tokenizer = load(model_path)
        self.policy = policy

        zero = self.tokenizer.encode("0", add_special_tokens=False)
        one = self.tokenizer.encode("1", add_special_tokens=False)
        if len(zero) != 1 or len(one) != 1:
            raise RuntimeError(f"'0'/'1' are not single tokens: {zero} {one}")
        self.zero_id, self.one_id = zero[0], one[0]

        # Token guard: production renders the prompt with plain transformers
        # (gpu/cope-b-runpod/prompt.py). If MLX's tokens differ, verdicts
        # silently drift (that is how thinking mode went unnoticed), so stop.
        hf = AutoTokenizer.from_pretrained(model_path)
        probe = "Token parity probe."
        hf_text = hf.apply_chat_template(
            [{"role": "user", "content": build_body(policy=policy, content=probe)}],
            tokenize=False, add_generation_prompt=True,
        )
        if hf.encode(hf_text, add_special_tokens=False) != self.prompt_tokens(probe):
            raise RuntimeError("MLX prompt tokens differ from the transformers prompt "
                             "production uses")

    def prompt_tokens(self, content: str) -> list[int]:
        text = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": build_body(policy=self.policy, content=content)}],
            tokenize=False,
            add_generation_prompt=True,
            # mlx-lm defaults thinking mode ON; production never sends it.
            enable_thinking=False,
        )
        # The chat template already emits <bos>; don't add a second one.
        return self.tokenizer.encode(text, add_special_tokens=False)

    def new_cache(self):
        return self._make_cache(self.model)

    def forward(self, tokens, cache):
        mx = self._mx
        logits = self.model(mx.array([tokens]), cache=cache)
        last = logits[0, -1, :].astype(mx.float32)
        logprobs = last - mx.logsumexp(last)
        return logprobs.tolist()

    def snapshot(self, cache):
        mx = self._mx
        snap = [layer.state for layer in cache]
        mx.eval([x for st in snap for x in st if isinstance(x, mx.array)])
        return snap

    def restore(self, snap):
        mx = self._mx
        cache = self._make_cache(self.model)
        for layer, st in zip(cache, snap):
            # mx.array(x) makes a new array object, so the in-place cache
            # update during a post cannot write through to the snapshot.
            layer.state = tuple(mx.array(x) if isinstance(x, mx.array) else x for x in st)
        return cache

    def decode(self, token_id: int) -> str:
        return self.tokenizer.decode([token_id])


def load_engine(model_path: str, policy_version: str, *, policy_path: Path = DEFAULT_POLICY_PATH,
                reuse_prefix: bool = True, model_name: str = LOCAL_MODEL_NAME) -> CopeBEngine:
    policy = Path(policy_path).read_text(encoding="utf-8")
    return CopeBEngine(MlxBackend(model_path, policy), model_name=model_name,
                       policy_version=policy_version, reuse_prefix=reuse_prefix)
