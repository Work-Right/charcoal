"""Local server: RunPod wire format, per-post isolation, startup refusals.

The server must look exactly like the RunPod worker to `RunPodCopeBClient`
(src/toxicity/runpod_cope_b.rs), so the Rust side reuses that client
unchanged (#437 spec D3). A fake engine stands in for the model.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from hypothesis import given, strategies as st

import server


class FakeEngine:
    """Returns a verdict derived from the content; raises on chosen inputs."""

    def __init__(self, fail_on=frozenset()):
        self.fail_on = fail_on

    def classify_one(self, content):
        if content in self.fail_on:
            raise RuntimeError(f"boom on {content!r}")
        return {"ok": True, "toxic": len(content) % 2 == 1, "confidence": 0.9,
                "model": "m", "policy_version": "p"}


# Invariant: one slot per input, in input order; a post that raises becomes
# ok:false for that post only, every other post is unaffected.
@given(st.lists(st.text(max_size=5), max_size=12), st.data())
def test_classify_batch_aligns_and_isolates_failures(contents, data):
    fail_on = frozenset(data.draw(st.sets(st.sampled_from(contents))) if contents else set())
    slots = server.classify_batch(FakeEngine(fail_on), contents)
    assert len(slots) == len(contents)
    for content, slot in zip(contents, slots):
        if content in fail_on:
            assert slot["ok"] is False and "boom" in slot["error"]
        else:
            assert slot == FakeEngine().classify_one(content)


@pytest.fixture
def running():
    httpd = server.make_server(FakeEngine(fail_on={"bad"}), host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer local"
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_runsync_returns_a_completed_runpod_envelope(running):
    # Requirement: RunPodCopeBClient reads id, status == COMPLETED,
    # output.verdicts, and the delayTime/executionTime timing fields.
    status, env = call(f"{running}/runsync", {"input": {"contents": ["ab", "bad", "c"]}})
    assert status == 200
    assert env["status"] == "COMPLETED" and env["id"]
    assert env["delayTime"] == 0 and isinstance(env["executionTime"], int)
    verdicts = env["output"]["verdicts"]
    assert [v["ok"] for v in verdicts] == [True, False, True]


def test_status_returns_the_same_envelope(running):
    # Requirement: the client polls /status/{id} if /runsync is not terminal;
    # it must get the same finished job back.
    _, env = call(f"{running}/runsync", {"input": {"contents": ["x"]}})
    status, again = call(f"{running}/status/{env['id']}")
    assert status == 200 and again == env


def test_missing_contents_is_a_client_error(running):
    # Requirement: a malformed request is a 4xx, which the client surfaces
    # immediately instead of retrying (it retries only 5xx).
    status, body = call(f"{running}/runsync", {"input": {"content": "x"}})
    assert status == 400 and "contents" in body["error"]


def test_unknown_job_and_path_are_404(running):
    assert call(f"{running}/status/nope")[0] == 404
    assert call(f"{running}/run", {"input": {"contents": []}})[0] == 404


def test_works_without_an_authorization_header(running):
    # Requirement (D6): loopback only, no auth; the placeholder key the
    # client sends is ignored, and its absence is fine too.
    assert call(f"{running}/runsync", {"input": {"contents": ["x"]}}, auth=False)[0] == 200


# Invariant (D6): only loopback addresses are accepted as a bind host.
@given(st.text(max_size=20))
def test_only_loopback_hosts_are_allowed(host):
    if host in ("127.0.0.1", "::1"):
        server.require_loopback(host)
    else:
        with pytest.raises(SystemExit):
            server.require_loopback(host)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_policy_version_is_required(value):
    # Requirement (D5): no default policy version; refuse to start without one.
    with pytest.raises(SystemExit, match="POLICY_VERSION"):
        server.require_policy_version(value)


def test_policy_version_is_trimmed():
    assert server.require_policy_version(" policy-x ") == "policy-x"
