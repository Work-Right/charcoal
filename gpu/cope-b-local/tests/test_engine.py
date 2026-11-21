"""Engine logic, tested against a fake backend so no model or MLX is needed.

The fake model's answer depends on EVERY token in its cache, so a cache that
still holds tokens from a previous post (the trim bug from #435) or that lost
part of the policy prefix produces a different answer, and these tests fail.
"""

from __future__ import annotations

import hashlib
import math

import pytest
from hypothesis import given, strategies as st

from engine import CopeBEngine, shared_prefix_len, slot_from_logprobs

ZERO, ONE = 10, 11  # fake token ids for "0" and "1"
VOCAB = 16


class FakeCache:
    def __init__(self, tokens=None):
        self.tokens = list(tokens or [])


class FakeBackend:
    """Tokenizes text one character per token, after a fixed policy prefix."""

    PREFIX = [1, 2, 3, 4, 5]
    SUFFIX = [6, 7]

    def __init__(self):
        self.forward_calls = 0

    def prompt_tokens(self, content: str) -> list[int]:
        return self.PREFIX + [100 + (ord(c) % 50) for c in content] + self.SUFFIX

    def new_cache(self):
        return FakeCache()

    def forward(self, tokens, cache):
        self.forward_calls += 1
        cache.tokens.extend(tokens)
        # Deterministic "logprobs" from the WHOLE cache, like real attention.
        digest = hashlib.sha256(repr(cache.tokens).encode()).digest()
        logits = [digest[i] / 25.0 for i in range(VOCAB)]
        logits[ZERO] += 5
        logits[ONE] += 5 if digest[0] % 2 else 0
        top = max(logits)
        lse = top + math.log(sum(math.exp(x - top) for x in logits))
        return [x - lse for x in logits]

    def snapshot(self, cache):
        return list(cache.tokens)

    def restore(self, snap):
        return FakeCache(snap)

    def decode(self, token_id: int) -> str:
        return {ZERO: "0", ONE: "1"}.get(token_id, "x")

    zero_id, one_id = ZERO, ONE


def make(reuse=True):
    return CopeBEngine(FakeBackend(), model_name="m", policy_version="p", reuse_prefix=reuse)


# Invariant: n is the length of the longest common prefix of a and b.
@given(st.lists(st.integers(0, 5)), st.lists(st.integers(0, 5)))
def test_shared_prefix_len_is_longest_common_prefix(a, b):
    n = shared_prefix_len(a, b)
    assert n <= min(len(a), len(b))
    assert a[:n] == b[:n]
    if n < min(len(a), len(b)):
        assert a[n] != b[n]


# Invariant: a post's verdict does not depend on which posts came before it.
# This is what the trim bug broke: leftovers from post A changed post B.
@given(st.lists(st.text(min_size=0, max_size=8), min_size=1, max_size=6))
def test_verdict_is_independent_of_earlier_posts(posts):
    engine = make()
    in_sequence = [engine.classify_one(p) for p in posts]
    alone = [make().classify_one(p) for p in posts]
    assert in_sequence == alone


# Invariant: reusing the prefix gives the same verdict as running the full
# prompt, so the cache is a pure speed-up.
@given(st.lists(st.text(max_size=8), min_size=1, max_size=6))
def test_prefix_reuse_matches_full_prompt(posts):
    cached, full = make(reuse=True), make(reuse=False)
    assert [cached.classify_one(p) for p in posts] == [full.classify_one(p) for p in posts]


def test_post_that_breaks_the_prefix_runs_in_full():
    # Requirement: a prompt that does not start with the shared prefix (e.g.
    # whitespace merging across the boundary) is classified from scratch,
    # not refused and not fed a wrong continuation.
    def broken(content):
        return [9] + FakeBackend.PREFIX + [100, 6, 7]

    cached, full = make(reuse=True), make(reuse=False)
    cached.backend.prompt_tokens = broken
    full.backend.prompt_tokens = broken
    assert cached.classify_one("x") == full.classify_one("x")
    assert cached.uncached_posts == 1


# Invariant: slot shape. "0"/"1" -> ok with confidence = exp(logprob of the
# emitted token) in (0, 1]; any other top token -> ok false with an error.
@given(st.lists(st.floats(-30, 0), min_size=VOCAB, max_size=VOCAB))
def test_slot_from_logprobs_shape(logprobs):
    slot = slot_from_logprobs(logprobs, FakeBackend().decode, ONE, "m", "p")
    top = max(range(VOCAB), key=lambda i: logprobs[i])
    if top in (ZERO, ONE):
        assert slot["ok"] is True
        assert slot["toxic"] is (top == ONE)
        assert slot["confidence"] == pytest.approx(math.exp(logprobs[top]))
        assert 0 < slot["confidence"] <= 1
        assert slot["model"] == "m" and slot["policy_version"] == "p"
    else:
        assert slot == {"ok": False, "error": "unexpected model token: 'x'"}
