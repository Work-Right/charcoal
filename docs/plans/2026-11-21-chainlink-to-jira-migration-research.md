# Plan: replace chainlink with Jira Cloud, and cut the ceremony

**Status:** recommendation, revised 2026-11-22. Nothing is migrated yet.
**Tracking:** chainlink #439
**Site:** `makeworkbetter.atlassian.net`. It's reachable through the Atlassian
MCP with Jira and Confluence read-write. No Charcoal project exists yet.

The first draft of this report (commit `c6092f6`) rebuilt chainlink's hooks
around Jira. On review, that just moved the ceremony to a new home. This
version keeps what's still worth having and deletes the rest.

---

## 1. The decision in one paragraph

Jira becomes the **only** issue tracker. Agents work in it directly through
the Atlassian MCP, and nothing syncs with a local copy. The "every piece of
work has an issue" rule is enforced **once, at merge time**: a CI check
requires a `CHAR-N` key in the pull request title. It is no longer checked on
every tool call. The git commands only you may run are blocked by **Claude
Code's own permission settings** instead of a custom hook. Every chainlink
hook, the session machinery and the issue-database backups are deleted. Old
`#N` references keep their meaning, because we import in order so that
**`#N` = `CHAR-N`**.

### Options considered

| Option | Verdict |
|---|---|
| Rebuild chainlink's hooks around Jira (first draft) | Rejected: same upkeep, new home |
| **Beads**: a local agent tracker with two-way `bd jira sync` | Rejected. It's chainlink plus a sync layer. Two copies of every issue, timestamp-based conflict resolution, and a third ID scheme (`bd-a1b2`) that breaks `#N` = `CHAR-N`. The gap it bridges (agents couldn't reach Jira) is closed by the MCP. |
| **Jira only, check at merge, native permissions** | **Chosen** |

---

## 2. What happens to each chainlink piece

| Piece | Fate | Why |
|---|---|---|
| Issues, comments, sub-issues, links (439 / 424 / 67 / 33) | **Import to Jira** | Real history; §5 |
| Issue check on every Write/Edit/Bash (`work-check.py`) | **Delete** | In practice it costs one command per session and catches nothing the merge check won't. What matters is that *shipped* work has an issue. |
| Git-mutation blocklist | **Move** to `permissions.deny` in `.claude/settings.json` | Claude Code enforces it natively, in every session, with no script to maintain |
| Bash allowlist | **Delete** | Without the issue check, there's nothing to be allowed past |
| Rules injected into every prompt (`prompt-guard.py`, `.chainlink/rules/`) | **Delete** | About 17 KB on the first prompt plus a reminder on every prompt. It repeats CLAUDE.md and adds generic advice current models already follow. Anything project-specific in `rust.md`/`project.md` that isn't in CLAUDE.md gets folded in first. |
| Session start / end / breadcrumbs (`session-start.py`) | **Delete** | Handoffs move to Jira (§4). Breadcrumbs are covered by deciduous and Claude Code compaction. |
| Lint + stub check after edit (`post-edit-check.py`) | **Delete** | The pre-push hook and CI already run clippy and the tests |
| Web-fetch rules + `safe-fetch` MCP (`pre-web-check.py`) | **Keep for now** | Prompt-injection defence is a separate question. Move `web.md` to `.claude/rules/` so it no longer lives in `.chainlink/`. Review separately. |
| Pre-commit chainlink export + `issues.db` R2 backup | **Delete** | Jira is the system of record. The deciduous backup stays. |
| Changelog line on `issue close` | **Delete** | It wrote entries backwards 7×. Handwritten entries continue, and the Release Notes Drafter can help if the plan includes Rovo. |

What's left to maintain: one CI check, one block of permission settings, and a
short section in CLAUDE.md.

---

## 3. Jira setup

- **Project:** `Charcoal`, key **`CHAR`**. Software, team-managed, **Kanban**
  (no sprints, and you administer it yourself).
- **Work types:** Epic, Task, Bug, Subtask, plus **Spike** for research that
  closes with a report rather than code.
- **Workflow:**
  `To Do → In Progress → In Review → On Staging → Done`.
  *On Staging* answers "what's merged but not live?" without
  `git log staging..main`.
- **GitHub for Jira** installed on `Work-Right/charcoal`. A key in the branch
  name, commit or PR title links the work to the item automatically.
- **Automation:** optional, depending on the plan. Free allows 150 steps a
  month in total (billed per step from 2026-12-03), which covers about one
  rule at our pace. Start with just **"PR merged into `main` → Done"**. Agents
  move items between the other statuses through the MCP as part of normal
  work. Add *In Review* and *On Staging* rules if the plan is Standard or
  higher.
- **Labels:** normalised during the import (`fix`→`bug`,
  `perf`→`performance`, `enhancement`→`feature`, …).
- **Saved filter "Ready":** `project = CHAR AND statusCategory = "To Do" AND
  Flagged is EMPTY ORDER BY priority DESC, created ASC`. Blocked items get the
  Flag. There are only 2 blockers in 439 issues, so no JQL gymnastics.

**Connector housekeeping:** the "claude.ai Atlassian Rovo" connector still
uses the deprecated v1 endpoint (removed 2027-03-01). Remove it, or re-add it
against `https://mcp.atlassian.com/v2/mcp`, so agents see one tool set, not
two.

---

## 4. The new discipline

### 4a. CLAUDE.md, the whole of it

Replace the chainlink section with roughly:

> **Issue tracking: Jira project `CHAR`** (via the Atlassian MCP).
> Before starting work, find the item (JQL) or create one. Put its key in the
> branch name and the PR title (`CHAR-123: …`). Move it to *In Progress* when
> you start. Before you stop, add a comment starting `Handoff:`: where things
> stand and what's next. When resuming work on an item, read its latest
> `Handoff:` comment first. Historical `#N` references mean `CHAR-N`.

That's the full "process". Current agents follow a clear instruction like
this. The CI check below catches the times they don't.

### 4b. The merge-time check (the one hard rule)

A GitHub Actions job runs on every PR. It fails unless the title contains
`CHAR-\d+`, with these exceptions:

- Promotion PRs (head branch `staging` → base `main`). Those bundle many
  items.
- Bot PRs (Dependabot).

Optionally it also confirms the key exists, using a read-only Jira API token
stored as a repo secret.

Per the testing rules, the title check is a pure function that parses
untrusted input. Put it in a small script, not inline in the YAML, and give it
a property test (e.g. "accepts iff some `CHAR-<digits>` token appears,
bounded by non-alphanumerics") and a fuzz seed set.

### 4c. Permission settings (replaces the git blocklist)

Add to `.claude/settings.json`:

```json
"permissions": {
  "deny": [
    "Bash(git rebase:*)", "Bash(git reset:*)", "Bash(git cherry-pick:*)",
    "Bash(git tag:*)", "Bash(git branch -D:*)", "Bash(git branch -m:*)",
    "Bash(git clean:*)", "Bash(git am:*)", "Bash(git apply:*)",
    "Bash(git checkout .:*)", "Bash(git restore .:*)"
  ]
}
```

⚠️ **Discrepancy to settle:** `.chainlink/hook-config.json` still lists
`git merge` as blocked, but memory says merges were allowed from PR #168 (the
work-check hook blocks any command *containing* "merge"). The list above
leaves `git merge` allowed, which matches the intended behaviour. Confirm.

### 4d. Handoffs: one home

A `Handoff:` comment on the Jira item. You can read it from your phone, it
lives with the work, and cloud sessions can see it, which the local memory
files can't. The "Next session — start here" block in `MEMORY.md` shrinks to
durable facts only, and stops duplicating per-item status.

---

## 5. Import plan (`#N` → `CHAR-N`)

**Why it works:** chainlink IDs are exactly 1–439 with no gaps, and every
sub-issue's parent has a lower ID. Creating items one at a time in ID order,
in a fresh project, therefore gives key N = #N, with parents always existing
before their children.

1. **Freeze.** Last `chainlink export`, plus one final R2 backup of
   `issues.db` under `history/`.
2. **Dry run in a scratch project `CHARX`.** A one-off Python script uses the
   REST API **sequentially** (not the 50-per-request bulk endpoint). For each
   issue it carries over:
   - title
   - description (Markdown → Jira format)
   - priority
   - normalised labels
   - a `cl-N` label
   - the parent
   - the original created/closed dates as a footer line (REST can't backdate
     "Created")

   Then it adds comments in order, each prefixed with its original timestamp,
   and moves closed items to Done. **It stops on the first key mismatch.**
   Jira never reuses keys, so a gap means deleting the scratch project and
   rerunning.
3. **Second pass** for the 31 relations and 2 blockers.
4. **Verify:** counts per status and label; `CHAR-N` title == `#N` title for
   every N; 20 random spot-checks of comments.
5. **Run it for real in `CHAR`** and verify again.

The alternative I rejected: Jira's CSV/JSON importer can backdate dates, but
it doesn't document whether it keeps creation order. Losing the 1:1 key
mapping costs more than losing exact dates.

---

## 6. Cutover: one PR, in this order

1. Import (§5) and confirm it.
2. `.claude/settings.json`:
   - add `permissions.deny` (§4c)
   - remove the `work-check`, `prompt-guard`, `session-start` and
     `post-edit-check` hook entries
   - point `pre-web-check` at `.claude/rules/web.md`

   In `.claude/settings.local.json`, drop the chainlink allows.
3. Delete those four hook scripts, `.chainlink/hook-config.json` and the
   `.chainlink/rules/` files no longer used. First fold any
   charcoal-specific rule text into CLAUDE.md.
4. `scripts/install-hooks.sh`: drop the chainlink export and the `issues.db`
   backup. `scripts/restore-dbs.sh` and `scripts/check-updates.sh`: drop the
   chainlink parts. `docs/chainlink-hook-cwd-fix.md`: mark it historical.
5. Add the CI title check (§4b), with its property test.
6. CLAUDE.md:
   - replace the chainlink section (lines 21, 49–53) with §4a
   - remove the chainlink notes at lines 200–201 and 212
7. Memory: replace the "No GitHub Issues / chainlink is the only tracker"
   entry with "Jira `CHAR` is the only tracker; `#N` = `CHAR-N`". Mark the
   chainlink gotcha entries as historical.
8. Leave the `.chainlink/` folder on disk, untracked, for a month, then
   delete it.

**Effort:** import script plus dry run, about half a day. Cutover PR, about
half a day. Net code change is strongly negative.

---

## 7. Decisions for you

1. **Template.** These hooks come from your project template, so the next
   template sync would put them back. Either remove them from the template
   (your other projects benefit too), or mark them as opted out for charcoal.
   *Recommend: remove from the template.*
2. **Plan level of `makeworkbetter`:** Free (no Rovo AI, about one automation
   rule) or Standard or higher? I couldn't read it from the MCP.
3. **Key `CHAR`**, team-managed Kanban: OK?
4. **`git merge`:** allowed (as in §4c) or blocked?
5. **Later, separately:** give deciduous's "log every action in real time"
   rule the same review.

---

## 8. Sources

- MCP v2 tools / auth / upgrade:
  developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/ ·
  …/authentication-and-authorization/ ·
  support.atlassian.com/atlassian-ai-gateway/docs/how-to-upgrade-from-atlassian-mcp-v1-to-atlassian-mcp-v2/
- Automation step billing: support.atlassian.com/cloud-automation/docs/how-is-my-usage-calculated/
- Linking dev work: support.atlassian.com/jira-software-cloud/docs/reference-issues-in-your-development-work/
- Rovo credits / agents: support.atlassian.com/rovo/docs/rovo-usage-limits/ ·
  support.atlassian.com/rovo/docs/atlassian-agents/
- Bulk create (50 max): developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-bulk-operations/
- Beads Jira sync: beads.gascity.com/cli-reference/jira.md ·
  steveyegge.github.io/beads/reference/faq

**Not verified:** MCP rate limits (community reports say ~500–1,000 calls an
hour). Whether the MCP fully works on a Free site. Whether team-managed
projects support components (not needed by this plan).
