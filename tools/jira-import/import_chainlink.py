#!/usr/bin/env python3
"""One-shot import of the chainlink issue database into a Jira Cloud project.

The whole point of this script is ONE invariant: chainlink issue #N becomes
Jira work item <PROJECT>-N. Every `#NNN` already written in the repo, commit
messages and memory files then stays readable as `CHAR-NNN`.

That works only because chainlink IDs are contiguous from 1 and every
sub-issue's parent has a lower ID. So creating items one at a time, in ID
order, into an EMPTY project gives key N for issue N, with parents always
existing first. Jira never reuses a key, so a single failed or duplicated
create would shift every later key. The script therefore checks the returned
key after every create and stops at the first mismatch. The recovery is to
delete the project, recreate it and start again.

See docs/plans/2026-11-21-chainlink-to-jira-migration-research.md (§5).

Commands:

    plan    No network. Reads the DB, prints counts and the type/label
            mapping, writes every payload to a JSONL file for inspection.
    run     Creates items in <PROJECT>. Safe to re-run after a crash: it
            picks up after the last item that matches, and fills in any
            missing comments or status on items that already exist.
    verify  Read-only. Checks every item's key, summary, status and comment
            count against the DB, and every link.

Auth: Jira REST API with Basic auth. JIRA_EMAIL is the Atlassian account
email. The token comes from JIRA_API_TOKEN, or is read with `op read` from
JIRA_TOKEN_REF (default op://apikeys/seshtest/credential).

stdlib only, so there's nothing to install to run it. The property tests
next to it need `hypothesis`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

SITE = "https://makeworkbetter.atlassian.net"
SUMMARY_MAX = 255  # Jira's hard limit on the summary field

# Label drift accumulated in chainlink (bug/fix, perf/performance, ...).
# Normalising here is cheaper than cleaning it up in Jira afterwards.
LABEL_ALIASES = {
    "fix": "bug",
    "perf": "performance",
    "enhancement": "feature",
    "cleanup": "tech-debt",
    "investigation": "research",
    "bugfix": "bug",
    "feat": "feature",
    "improvement": "feature",
    "arch": "architecture",
    "testing": "test",
    "ops": "infra",
    "devops": "infra",
}

# Priorities that were typed as labels by mistake. Priority is its own field
# in Jira, so these are dropped rather than imported.
DROPPED_LABELS = {"critical", "high", "medium", "low"}

SPIKE_LABELS = {"research", "spike"}

PRIORITIES = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


# ── Data loaded from chainlink ──────────────────────────────────────────


@dataclass
class Comment:
    content: str
    created_at: str
    kind: str


@dataclass
class Issue:
    id: int
    title: str
    description: str
    status: str
    priority: str
    parent_id: int | None
    created_at: str
    closed_at: str | None
    labels: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)


def load_db(path: str) -> tuple[list[Issue], list[tuple[int, int]], list[tuple[int, int]]]:
    """Return (issues sorted by id, relations, blocker→blocked pairs)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    issues = {
        r["id"]: Issue(
            id=r["id"],
            title=r["title"],
            description=r["description"] or "",
            status=r["status"],
            priority=r["priority"],
            parent_id=r["parent_id"],
            created_at=r["created_at"],
            closed_at=r["closed_at"],
        )
        for r in con.execute("SELECT * FROM issues ORDER BY id")
    }
    for r in con.execute("SELECT issue_id, label FROM labels ORDER BY label"):
        issues[r["issue_id"]].labels.append(r["label"])
    # Order by id, not created_at: comment ids are assigned in insertion
    # order, which is the order a reader saw them in.
    for r in con.execute("SELECT issue_id, content, created_at, kind FROM comments ORDER BY id"):
        issues[r["issue_id"]].comments.append(
            Comment(r["content"], r["created_at"], r["kind"] or "note")
        )
    relations = [(r[0], r[1]) for r in con.execute("SELECT issue_id_1, issue_id_2 FROM relations ORDER BY 1, 2")]
    blocks = [(r[0], r[1]) for r in con.execute("SELECT blocker_id, blocked_id FROM dependencies ORDER BY 1, 2")]
    con.close()
    return [issues[k] for k in sorted(issues)], relations, blocks


def check_preconditions(issues: list[Issue]) -> None:
    """Refuse to run if the #N == KEY-N mapping cannot hold."""
    ids = [i.id for i in issues]
    if ids != list(range(1, len(ids) + 1)):
        raise SystemExit("chainlink IDs are not contiguous from 1; #N == KEY-N cannot hold")
    for i in issues:
        if i.parent_id is not None and i.parent_id >= i.id:
            raise SystemExit(f"#{i.id} has parent #{i.parent_id} with a higher ID; ordered import would fail")


# ── Pure mapping functions (property-tested in test_import_chainlink.py) ─


def normalize_labels(labels: list[str]) -> list[str]:
    """Aliases folded, lowercased, whitespace → '-', deduped, sorted.

    Jira labels cannot contain spaces, so any whitespace run becomes '-'.
    Empty results and priority-named labels are dropped.
    """
    out = set()
    for raw in labels:
        lab = re.sub(r"\s+", "-", raw.strip().lower())
        lab = LABEL_ALIASES.get(lab, lab)
        if lab and lab not in DROPPED_LABELS:
            out.add(lab)
    return sorted(out)


def depths(issues: list[Issue]) -> dict[int, int]:
    """Nesting depth of each issue: 1 for top level, parent's depth + 1 otherwise.

    Relies on parents having lower IDs (checked in check_preconditions), so a
    single pass in ID order sees every parent first.
    """
    d: dict[int, int] = {}
    for i in issues:
        d[i.id] = 1 if i.parent_id is None else d[i.parent_id] + 1
    return d


def work_type(labels: list[str], depth: int, has_children: bool) -> str:
    """Jira work type for an issue.

    Jira's team-managed hierarchy is Epic > standard type > Subtask, so a
    top-level parent must be an Epic and anything at depth 3 must be a
    Subtask. Everything else is chosen from the (normalised) labels.
    """
    if depth >= 3:
        return "Subtask"
    if depth == 1 and has_children:
        return "Epic"
    if "bug" in labels:
        return "Bug"
    if SPIKE_LABELS & set(labels):
        return "Spike"
    return "Task"


def summary_for(title: str) -> str:
    """Title cut to Jira's 255-char limit. Overlong titles end in '…'."""
    title = " ".join(title.split())  # Jira rejects newlines in summaries
    if len(title) <= SUMMARY_MAX:
        return title
    return title[: SUMMARY_MAX - 1].rstrip() + "…"


_FENCE = re.compile(r"^```([\w+-]*)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Inline tokens, tried left to right: `code`, **bold**, [text](http-url).
_INLINE = re.compile(r"`([^`\n]+)`|\*\*(.+?)\*\*|\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def md_to_adf(text: str) -> list[dict]:
    """Convert the Markdown chainlink stored into ADF block nodes.

    ADF (Atlassian Document Format) is structured JSON in which every text
    node is literal. Unlike Jira's wiki markup, a stray `[`, `{` or leading
    `#` in our text (81, 22 and 19 cases respectively) can't be misread as a
    link, macro or list. Only the constructs our issues actually use are
    recognised: fenced code, headings, bullet and numbered lists, bold,
    inline code and links. Everything else is kept as plain text.
    Consecutive lines become one paragraph joined by hard breaks, and blank
    lines separate paragraphs. List nesting is flattened (ADF requires
    nested lists inside list items, and our data has almost none).
    """
    blocks: list[dict] = []
    para: list[str] = []
    items: list[str] = []
    list_kind = ""
    code: list[str] | None = None
    lang = ""

    def flush_para():
        if para:
            blocks.append({"type": "paragraph", "content": _lines_inline(para)})
            para.clear()

    def flush_list():
        nonlocal list_kind
        if items:
            blocks.append({
                "type": list_kind,
                "content": [{"type": "listItem", "content": [_paragraph(it)]} for it in items],
            })
            items.clear()
        list_kind = ""

    for line in text.split("\n"):
        if code is not None:
            if _FENCE.match(line.strip()):
                blocks.append(_code_block(code, lang))
                code = None
            else:
                code.append(line)
            continue
        fence = _FENCE.match(line.strip())
        if fence:
            flush_para(); flush_list()
            code, lang = [], fence.group(1)
            continue
        if not line.strip():
            flush_para(); flush_list()
            continue
        h = _HEADING.match(line)
        b = _BULLET.match(line)
        n = _NUMBERED.match(line)
        if h and h.group(2).strip():
            flush_para(); flush_list()
            blocks.append({"type": "heading", "attrs": {"level": len(h.group(1))},
                           "content": _inline(h.group(2))})
        elif (b or n) and (b or n).group(1).strip():
            kind = "bulletList" if b else "orderedList"
            flush_para()
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            items.append((b or n).group(1))
        else:
            flush_list()
            para.append(line)
    if code is not None:  # unterminated fence: keep what we have as code
        blocks.append(_code_block(code, lang))
    flush_para(); flush_list()
    return blocks


def _code_block(lines: list[str], lang: str) -> dict:
    node: dict = {"type": "codeBlock"}
    if lang:
        node["attrs"] = {"language": lang}
    body = "\n".join(lines)
    if body:  # ADF rejects empty text nodes
        node["content"] = [{"type": "text", "text": body}]
    return node


def _paragraph(s: str) -> dict:
    content = _inline(s)
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def _lines_inline(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for k, line in enumerate(lines):
        if k:
            out.append({"type": "hardBreak"})
        out.extend(_inline(line))
    return out


def _text(s: str, marks: list[dict] | None = None) -> list[dict]:
    if not s:
        return []
    node: dict = {"type": "text", "text": s}
    if marks:
        node["marks"] = marks
    return [node]


def _inline(s: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(s):
        out += _text(s[pos:m.start()])
        if m.group(1) is not None:
            out += _text(m.group(1), [{"type": "code"}])
        elif m.group(2) is not None:
            out += _text(m.group(2), [{"type": "strong"}])
        else:
            out += _text(m.group(3), [{"type": "link", "attrs": {"href": m.group(4)}}])
        pos = m.end()
    out += _text(s[pos:])
    return out


def _doc(blocks: list[dict]) -> dict:
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph"}]}


def description_for(issue: Issue) -> dict:
    """ADF body plus a provenance footer (REST cannot backdate 'Created')."""
    blocks = md_to_adf(issue.description) if issue.description.strip() else []
    if blocks:
        blocks.append({"type": "rule"})
    if summary_for(issue.title) != " ".join(issue.title.split()):
        blocks.append({"type": "paragraph", "content":
                       _text("Full title: ", [{"type": "strong"}]) + _text(issue.title)})
    note = f"Imported from chainlink #{issue.id}. Created {issue.created_at}"
    if issue.closed_at:
        note += f", closed {issue.closed_at}"
    blocks.append({"type": "paragraph", "content": _text(note + ".", [{"type": "em"}])})
    return _doc(blocks)


def comment_body(c: Comment) -> dict:
    kind = "" if c.kind == "note" else f" ({c.kind})"
    head = {"type": "paragraph", "content": _text(f"Originally posted {c.created_at}{kind}:", [{"type": "em"}])}
    return _doc([head] + md_to_adf(c.content))


def expected_key(project: str, issue_id: int) -> str:
    return f"{project}-{issue_id}"


def key_number(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


def create_payload(issue: Issue, project: str, type_ids: dict[str, str],
                   depth: int, has_children: bool) -> dict:
    labels = normalize_labels(issue.labels)
    fields = {
        "project": {"key": project},
        "issuetype": {"id": type_ids[work_type(labels, depth, has_children)]},
        "summary": summary_for(issue.title),
        "description": description_for(issue),
        "labels": labels,
        "priority": {"name": PRIORITIES.get(issue.priority, "Medium")},
    }
    if issue.parent_id is not None:
        fields["parent"] = {"key": expected_key(project, issue.parent_id)}
    return {"fields": fields}


# ── Jira REST client (v3 for anything carrying ADF bodies) ─────────────


class Jira:
    def __init__(self, email: str, token: str, site: str = SITE):
        self.site = site
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {cred}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def call(self, method: str, path: str, body: dict | None = None, ok404: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(8):
            req = urllib.request.Request(self.site + path, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok404:
                    return None
                if e.code in (429, 502, 503, 504):
                    wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                    print(f"  … {e.code}, retrying in {wait:.0f}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                detail = e.read().decode(errors="replace")[:500]
                raise SystemExit(f"{method} {path} → HTTP {e.code}: {detail}")
        raise SystemExit(f"{method} {path}: gave up after repeated rate limiting")

    def search_all(self, jql: str, fields: str) -> list[dict]:
        out, token = [], None
        while True:
            q = {"jql": jql, "fields": fields, "maxResults": "100"}
            if token:
                q["nextPageToken"] = token
            page = self.call("GET", "/rest/api/3/search/jql?" + urllib.parse.urlencode(q))
            out.extend(page.get("issues", []))
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return out


def jira_from_env() -> Jira:
    email = os.environ.get("JIRA_EMAIL")
    if not email:
        raise SystemExit("Set JIRA_EMAIL to the Atlassian account email the API token belongs to.")
    token = os.environ.get("JIRA_API_TOKEN")
    if not token:
        ref = os.environ.get("JIRA_TOKEN_REF", "op://apikeys/seshtest/credential")
        token = subprocess.run(["op", "read", ref], check=True, capture_output=True, text=True).stdout.strip()
    return Jira(email, token)


# ── Commands ────────────────────────────────────────────────────────────


def build_all(issues, project, type_ids):
    d = depths(issues)
    parents = {i.parent_id for i in issues if i.parent_id is not None}
    return [create_payload(i, project, type_ids, d[i.id], i.id in parents) for i in issues]


def cmd_plan(args) -> None:
    issues, relations, blocks = load_db(args.db)
    check_preconditions(issues)
    fake_ids = {t: t for t in ("Epic", "Task", "Bug", "Spike", "Subtask")}
    payloads = build_all(issues, args.project, fake_ids)
    with open(args.out, "w", encoding="utf-8") as f:
        for p in payloads:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    types: dict[str, int] = {}
    for p in payloads:
        t = p["fields"]["issuetype"]["id"]
        types[t] = types.get(t, 0) + 1
    labels: dict[str, int] = {}
    for p in payloads:
        for lab in p["fields"]["labels"]:
            labels[lab] = labels.get(lab, 0) + 1
    print(f"issues: {len(issues)} ({sum(i.status == 'closed' for i in issues)} closed)")
    print(f"comments: {sum(len(i.comments) for i in issues)}  relations: {len(relations)}  blocks: {len(blocks)}")
    print("work types:", dict(sorted(types.items())))
    print("truncated titles:", [i.id for i in issues if summary_for(i.title) != ' '.join(i.title.split())])
    print("labels:", ", ".join(f"{k}={v}" for k, v in sorted(labels.items(), key=lambda kv: -kv[1])))
    print(f"payloads → {args.out}")


def type_ids_for(jira: Jira, project: str) -> dict[str, str]:
    meta = jira.call("GET", f"/rest/api/2/issue/createmeta/{project}/issuetypes")
    ids = {t["name"]: t["id"] for t in meta.get("issueTypes", meta.get("values", []))}
    missing = {"Epic", "Task", "Bug", "Spike", "Subtask"} - ids.keys()
    if missing:
        raise SystemExit(f"{project} is missing work types: {sorted(missing)}")
    return ids


def existing_items(jira: Jira, project: str) -> dict[int, dict]:
    rows = jira.search_all(f"project = {project} ORDER BY key ASC", "summary,status,comment,issuelinks")
    return {key_number(r["key"]): r for r in rows}


def transition_to_done(jira: Jira, key: str) -> None:
    ts = jira.call("GET", f"/rest/api/2/issue/{key}/transitions")["transitions"]
    done = [t for t in ts if t["to"]["name"] == "Done"]
    if not done:
        raise SystemExit(f"{key}: no transition to Done available")
    jira.call("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": done[0]["id"]}})


def sync_item(jira: Jira, key: str, issue: Issue, have_comments: int, is_done: bool) -> None:
    """Bring an existing item's comments and status up to date with the DB.

    Comments are only ever appended in order, so 'have N of M' means the
    first N are in and the rest are missing. This is what makes a crash
    between create and comments recoverable.
    """
    for c in issue.comments[have_comments:]:
        jira.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": comment_body(c)})
    if issue.status == "closed" and not is_done:
        transition_to_done(jira, key)


def cmd_run(args) -> None:
    issues, relations, blocks = load_db(args.db)
    check_preconditions(issues)
    jira = jira_from_env()
    type_ids = type_ids_for(jira, args.project)
    payloads = build_all(issues, args.project, type_ids)

    have = existing_items(jira, args.project)
    if have:
        top = max(have)
        if sorted(have) != list(range(1, top + 1)):
            raise SystemExit(f"{args.project} has gaps in its keys; delete and recreate the project")
        for n, row in have.items():
            if row["fields"]["summary"] != payloads[n - 1]["fields"]["summary"]:
                raise SystemExit(f"{row['key']} summary does not match #{n}; refusing to continue")
        print(f"resuming: {top} items already present")

    limit = args.limit or len(issues)
    for issue, payload in zip(issues[:limit], payloads[:limit]):
        key = expected_key(args.project, issue.id)
        row = have.get(issue.id)
        if row is None:
            got = jira.call("POST", "/rest/api/3/issue", payload)["key"]
            if got != key:
                raise SystemExit(
                    f"STOP: created {got} for chainlink #{issue.id} (expected {key}). "
                    "The #N == KEY-N mapping is broken; delete and recreate the project."
                )
            sync_item(jira, key, issue, 0, False)
        else:
            c = row["fields"]["comment"]
            sync_item(jira, key, issue, c["total"],
                      row["fields"]["status"]["statusCategory"]["key"] == "done")
        if issue.id % 25 == 0:
            print(f"  {key}")

    if limit == len(issues):
        link_all(jira, args.project, relations, blocks)
    print(f"done: {limit} items in {args.project}")


def link_all(jira: Jira, project: str, relations, blocks) -> None:
    have = existing_items(jira, project)
    present = set()
    for n, row in have.items():
        for link in row["fields"].get("issuelinks", []):
            other = link.get("outwardIssue") or link.get("inwardIssue")
            present.add(frozenset((n, key_number(other["key"]))))
    wanted = [("Relates", a, b) for a, b in relations] + [("Blocks", a, b) for a, b in blocks]
    for name, a, b in wanted:
        if frozenset((a, b)) in present:
            continue
        jira.call("POST", "/rest/api/2/issueLink", {
            "type": {"name": name},
            "outwardIssue": {"key": expected_key(project, a)},
            "inwardIssue": {"key": expected_key(project, b)},
        })
        present.add(frozenset((a, b)))


def cmd_verify(args) -> None:
    issues, relations, blocks = load_db(args.db)
    jira = jira_from_env()
    have = existing_items(jira, args.project)
    fake_ids = {t: t for t in ("Epic", "Task", "Bug", "Spike", "Subtask")}
    payloads = build_all(issues, args.project, fake_ids)
    problems = []
    for issue, p in zip(issues, payloads):
        row = have.get(issue.id)
        if row is None:
            problems.append(f"#{issue.id}: missing")
            continue
        f = row["fields"]
        if f["summary"] != p["fields"]["summary"]:
            problems.append(f"#{issue.id}: summary mismatch")
        if f["comment"]["total"] != len(issue.comments):
            problems.append(f"#{issue.id}: {f['comment']['total']} comments, expected {len(issue.comments)}")
        done = f["status"]["statusCategory"]["key"] == "done"
        if done != (issue.status == "closed"):
            problems.append(f"#{issue.id}: status {f['status']['name']}, chainlink {issue.status}")
    extra = sorted(set(have) - {i.id for i in issues})
    if extra:
        problems.append(f"extra items in Jira: {extra[:10]}")
    links = sum(len(r["fields"].get("issuelinks", [])) for r in have.values()) // 2
    if links != len(relations) + len(blocks):
        problems.append(f"{links} links in Jira, expected {len(relations) + len(blocks)}")
    print(f"checked {len(issues)} issues, {links} links: {len(problems)} problem(s)")
    for p in problems[:50]:
        print("  " + p)
    sys.exit(1 if problems else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=".chainlink/issues.db")
    ap.add_argument("--project", required=True, help="Jira project key, e.g. CHARX for the dry run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "jira-import-plan.jsonl"))
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, help="only the first N issues (smoke test)")
    sub.add_parser("verify")
    args = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
