"""Property tests for the pure mapping functions in import_chainlink.py.

Run:  python3 -m venv "$TMPDIR/jira-venv" && "$TMPDIR/jira-venv/bin/pip" install hypothesis
      "$TMPDIR/jira-venv/bin/python" -m unittest tools/jira-import/test_import_chainlink.py
"""

import os
import sys
import unittest

from hypothesis import given, settings, strategies as st

sys.path.insert(0, os.path.dirname(__file__))
import import_chainlink as ic  # noqa: E402

settings.register_profile("ci", max_examples=400, deadline=None)
settings.load_profile("ci")

label_text = st.one_of(
    st.sampled_from(sorted(ic.LABEL_ALIASES) + sorted(ic.DROPPED_LABELS) + ["bug", "Feature", " docs "]),
    st.text(min_size=0, max_size=20),
)
# Prose that may contain any character Jira's wiki markup would misread
# ([, {, #, |, _ …) but none of the Markdown triggers we parse.
plain_line = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
                           whitelist_characters=".,;:!?'\"()[]{}#|_~^+=<>/\\@$%&"),
    min_size=1, max_size=60,
).filter(lambda s: s.strip() and not s.lstrip()[:1] in "#-*" and not ic._NUMBERED.match(s)
         and not ic._INLINE.search(s) and not ic._FENCE.match(s.strip()))
plain = plain_line
# Markdown drops whitespace right after a list/heading marker, so text used
# as an item or heading must not start or end with whitespace.
trimmed_line = plain_line.filter(lambda s: s == s.strip())
code_line = st.text(alphabet=st.characters(blacklist_characters="\n\r", blacklist_categories=("Cs",)), max_size=60) \
    .filter(lambda s: not ic._FENCE.match(s.strip()))

BLOCK = {"paragraph", "heading", "bulletList", "orderedList", "codeBlock", "rule"}
INLINE = {"text", "hardBreak"}


def assert_well_formed(tc, blocks):
    """ADF shape rules Jira enforces (it rejects the whole request otherwise)."""
    for b in blocks:
        tc.assertIn(b["type"], BLOCK)
        if b["type"] in ("paragraph", "heading"):
            for n in b.get("content", []):
                tc.assertIn(n["type"], INLINE)
                if n["type"] == "text":
                    tc.assertTrue(n["text"])  # empty text nodes are rejected
        elif b["type"] in ("bulletList", "orderedList"):
            tc.assertTrue(b["content"])
            for item in b["content"]:
                tc.assertEqual(item["type"], "listItem")
                assert_well_formed(tc, item["content"])
        elif b["type"] == "codeBlock":
            for n in b.get("content", []):
                tc.assertEqual(n["type"], "text")
                tc.assertTrue(n["text"])


def flat(blocks):
    """Visible text: blocks joined by blank lines, hard breaks as newlines."""
    out = []
    for b in blocks:
        if b["type"] in ("bulletList", "orderedList"):
            out.append("\n".join(flat(i["content"]) for i in b["content"]))
        elif b["type"] == "rule":
            out.append("----")
        else:
            out.append("".join(n.get("text", "\n") for n in b.get("content", [])))
    return "\n\n".join(out)


class NormalizeLabels(unittest.TestCase):
    # Invariant: output is sorted, unique, lowercase, whitespace-free, and
    # contains no alias source and no priority-named label.
    @given(st.lists(label_text, max_size=8))
    def test_output_is_canonical(self, labels):
        out = ic.normalize_labels(labels)
        self.assertEqual(out, sorted(set(out)))
        for lab in out:
            self.assertTrue(lab)
            self.assertEqual(lab, lab.lower())
            self.assertFalse(any(ch.isspace() for ch in lab))
            self.assertNotIn(lab, ic.LABEL_ALIASES)
            self.assertNotIn(lab, ic.DROPPED_LABELS)

    # Invariant: normalising twice changes nothing.
    @given(st.lists(label_text, max_size=8))
    def test_idempotent(self, labels):
        once = ic.normalize_labels(labels)
        self.assertEqual(ic.normalize_labels(once), once)

    # Invariant: the order labels were added in doesn't matter.
    @given(st.lists(label_text, max_size=8), st.randoms())
    def test_order_insensitive(self, labels, rnd):
        shuffled = list(labels)
        rnd.shuffle(shuffled)
        self.assertEqual(ic.normalize_labels(shuffled), ic.normalize_labels(labels))

    # Invariant: every alias lands on its target (so e.g. 'fix' and 'bug'
    # end up as one Jira label).
    @given(st.sampled_from(sorted(ic.LABEL_ALIASES)))
    def test_alias_maps_to_target(self, alias):
        self.assertEqual(ic.normalize_labels([alias.upper()]), [ic.LABEL_ALIASES[alias]])


class WorkType(unittest.TestCase):
    # Invariant: the type always fits Jira's 3-level hierarchy. Depth 3+ is
    # Subtask, a top-level parent is Epic, and nothing else is ever either.
    # Below that, 'bug' wins over research labels, which win over Task.
    @given(st.lists(st.sampled_from(["bug", "research", "spike", "feature", "docs"]), unique=True),
           st.integers(min_value=1, max_value=5), st.booleans())
    def test_hierarchy_and_labels(self, labels, depth, has_children):
        t = ic.work_type(labels, depth, has_children)
        if depth >= 3:
            self.assertEqual(t, "Subtask")
        elif depth == 1 and has_children:
            self.assertEqual(t, "Epic")
        else:
            self.assertNotIn(t, ("Epic", "Subtask"))
            if "bug" in labels:
                self.assertEqual(t, "Bug")
            elif {"research", "spike"} & set(labels):
                self.assertEqual(t, "Spike")
            else:
                self.assertEqual(t, "Task")


class Summary(unittest.TestCase):
    # Invariant: the summary fits Jira (<= 255 chars, no newlines). A title
    # that already fits passes through with whitespace collapsed. A longer one
    # is a prefix of the collapsed title, followed by '…'.
    @given(st.text(max_size=600))
    def test_fits_and_preserves(self, title):
        s = ic.summary_for(title)
        collapsed = " ".join(title.split())
        self.assertLessEqual(len(s), ic.SUMMARY_MAX)
        self.assertNotIn("\n", s)
        if len(collapsed) <= ic.SUMMARY_MAX:
            self.assertEqual(s, collapsed)
        else:
            self.assertTrue(s.endswith("…"))
            self.assertTrue(collapsed.startswith(s[:-1]))


class MdToAdf(unittest.TestCase):
    # Invariant: for ANY input the output is valid ADF (no empty text nodes,
    # inline nodes only where allowed). Jira rejects the request otherwise.
    @given(st.lists(st.one_of(st.just("```"), st.just("```rust"), st.just(""), st.just("- item"),
                              st.just("1. step"), st.just("## Head"), st.just("**b** `c`"), code_line),
                    max_size=15))
    def test_always_well_formed(self, lines):
        assert_well_formed(self, ic.md_to_adf("\n".join(lines)))

    # Invariant: plain prose comes through character for character, including
    # the [, {, # and | that broke the wiki-markup approach. Lines become hard
    # breaks, and blank lines separate paragraphs.
    @given(st.lists(st.lists(plain_line, min_size=1, max_size=4), min_size=1, max_size=4))
    def test_plain_text_is_literal(self, paras):
        text = "\n\n".join("\n".join(p) for p in paras)
        self.assertEqual(flat(ic.md_to_adf(text)), text)

    # Invariant: every line inside a fenced block comes through byte-for-byte
    # in one codeBlock, with the fence's language.
    @given(st.lists(code_line, min_size=1, max_size=6), st.sampled_from(["", "rust", "sql"]))
    def test_fenced_code_preserved(self, lines, lang):
        body = "\n".join(lines)
        blocks = ic.md_to_adf(f"before\n```{lang}\n{body}\n```\nafter")
        code = [b for b in blocks if b["type"] == "codeBlock"]
        self.assertEqual(len(code), 1)
        self.assertEqual("".join(n["text"] for n in code[0].get("content", [])), body)
        self.assertEqual(code[0].get("attrs", {}).get("language", ""), lang)

    # Invariant: inline code becomes a code-marked text node with its body
    # intact, and nothing inside it is parsed as bold or a link.
    @given(st.text(alphabet=st.characters(blacklist_characters="`\n\r", blacklist_categories=("Cs",)), min_size=1, max_size=30))
    def test_inline_code(self, body):
        nodes = ic.md_to_adf(f"x `{body}` y")[0]["content"]
        self.assertEqual(nodes[1], {"type": "text", "text": body, "marks": [{"type": "code"}]})
        self.assertEqual(flat(ic.md_to_adf(f"x `{body}` y")), f"x {body} y")

    # Invariant: a Markdown link keeps its text, and its URL becomes the href.
    @given(plain_line.filter(lambda s: "]" not in s),
           st.from_regex(r"https://[a-z]{1,10}\.[a-z]{2,4}/[a-z0-9/]{0,20}", fullmatch=True))
    def test_links(self, text, url):
        nodes = ic.md_to_adf(f"[{text}]({url})")[0]["content"]
        self.assertEqual(nodes, [{"type": "text", "text": text, "marks": [{"type": "link", "attrs": {"href": url}}]}])

    # Invariant: '#'*n + space + text is a level-n heading with that text.
    @given(st.integers(min_value=1, max_value=6), trimmed_line)
    def test_headings(self, n, text):
        b = ic.md_to_adf("#" * n + " " + text)[0]
        self.assertEqual((b["type"], b["attrs"]["level"], flat([b])), ("heading", n, text))

    # Invariant: a run of '- ' lines is one bullet list, one item per line,
    # in order, with the item text intact.
    @given(st.lists(trimmed_line, min_size=1, max_size=6), st.sampled_from(["- ", "* ", "1. "]))
    def test_lists(self, items, marker):
        blocks = ic.md_to_adf("\n".join(marker + it for it in items))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "orderedList" if marker == "1. " else "bulletList")
        self.assertEqual([flat(i["content"]) for i in blocks[0]["content"]], items)


@st.composite
def forests(draw):
    """Chainlink-shaped data: ids 1..n, each parent id lower than the child's."""
    n = draw(st.integers(min_value=1, max_value=40))
    issues = []
    for i in range(1, n + 1):
        parent = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=i - 1))) if i > 1 else None
        issues.append(ic.Issue(i, f"t{i}", "", draw(st.sampled_from(["open", "closed"])),
                               draw(st.sampled_from(sorted(ic.PRIORITIES))), parent,
                               "2026-01-01T00:00:00Z", None))
    return issues


class Structure(unittest.TestCase):
    # Invariant: depth is 1 at the top and parent depth + 1 below.
    @given(forests())
    def test_depths(self, issues):
        d = ic.depths(issues)
        for i in issues:
            self.assertEqual(d[i.id], 1 if i.parent_id is None else d[i.parent_id] + 1)

    # Invariant: preconditions accept exactly the shapes the ordered import
    # can handle: contiguous ids from 1, and parents lower than children.
    @given(forests())
    def test_preconditions_accept_valid(self, issues):
        ic.check_preconditions(issues)

    @given(forests(), st.integers(min_value=0, max_value=39))
    def test_preconditions_reject_gap(self, issues, k):
        issues[k % len(issues)].id += 1000
        with self.assertRaises(SystemExit):
            ic.check_preconditions(sorted(issues, key=lambda i: i.id))

    @given(forests().filter(lambda f: len(f) >= 2), st.integers(min_value=0, max_value=38))
    def test_preconditions_reject_forward_parent(self, issues, k):
        child = issues[k % (len(issues) - 1)]
        child.parent_id = child.id + 1  # a parent that would not exist yet
        with self.assertRaises(SystemExit):
            ic.check_preconditions(issues)

    # Invariant: every payload's parent is PROJECT-<parent id> (that's what
    # makes ordered creation link parents correctly), and its summary fits Jira.
    @given(forests())
    def test_payload_parent_key(self, issues):
        ids = {t: t for t in ("Epic", "Task", "Bug", "Spike", "Subtask")}
        for issue, p in zip(issues, ic.build_all(issues, "CHARX", ids)):
            f = p["fields"]
            if issue.parent_id is None:
                self.assertNotIn("parent", f)
            else:
                self.assertEqual(f["parent"]["key"], f"CHARX-{issue.parent_id}")
            self.assertLessEqual(len(f["summary"]), ic.SUMMARY_MAX)
            assert_well_formed(self, f["description"]["content"])
            self.assertIn(f"chainlink #{issue.id}", flat(f["description"]["content"]))

    # Invariant: blocks_in_jira reads Jira's GET shape back into exactly the
    # (blocker, blocked) pairs, with no reversal. Each link shows on both
    # items: the blocker sees it as outwardIssue, the blocked as inwardIssue.
    # Relates links are ignored.
    @given(st.sets(st.tuples(st.integers(1, 30), st.integers(1, 30)).filter(lambda t: t[0] != t[1]), max_size=10),
           st.sets(st.tuples(st.integers(1, 30), st.integers(1, 30)), max_size=5))
    def test_blocks_direction(self, blocks, relates):
        have = {n: {"fields": {"issuelinks": []}} for n in range(1, 31)}
        k = lambda n: f"CHARX-{n}"
        for a, b in blocks:
            have[a]["fields"]["issuelinks"].append({"type": {"name": "Blocks"}, "outwardIssue": {"key": k(b)}})
            have[b]["fields"]["issuelinks"].append({"type": {"name": "Blocks"}, "inwardIssue": {"key": k(a)}})
        for a, b in relates:
            have[a]["fields"]["issuelinks"].append({"type": {"name": "Relates"}, "outwardIssue": {"key": k(b)}})
        self.assertEqual(ic.blocks_in_jira(have), blocks)

    # Invariant: key_number inverts expected_key.
    @given(st.from_regex(r"[A-Z][A-Z0-9]{1,9}", fullmatch=True), st.integers(min_value=1, max_value=10**6))
    def test_key_roundtrip(self, project, n):
        self.assertEqual(ic.key_number(ic.expected_key(project, n)), n)

    # Invariant: an overlong title is never lost. The full text goes into the
    # description.
    @given(st.text(alphabet="abc ", min_size=300, max_size=400).filter(lambda s: len(" ".join(s.split())) > 255))
    def test_long_title_kept_in_description(self, title):
        issue = ic.Issue(1, title, "", "open", "low", None, "2026-01-01", None)
        self.assertIn(title, flat(ic.description_for(issue)["content"]))


if __name__ == "__main__":
    unittest.main()
