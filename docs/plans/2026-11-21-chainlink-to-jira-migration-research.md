# Research: moving Charcoal's issue tracking from chainlink to Jira Cloud

**Status:** research / recommendation. Nothing is migrated yet.
**Date:** 2026-11-21 · chainlink #439 (fittingly, the last one we'd need)
**Site:** `makeworkbetter.atlassian.net` (reachable via the Atlassian MCP; Jira
and Confluence are read-write; **no Charcoal project exists yet**)

---

## 1. TL;DR

1. **Separate chainlink's two jobs.** Only about half of what chainlink does
   here is issue tracking. The other half is agent guardrails: the git
   blocklist, coding-rules injection, stub/lint checks and web-fetch rules.
   Those hooks barely touch the chainlink binary. Keep them, move them out of
   `.chainlink/`, and replace only the parts that are really about tracking.
2. **Keep the issue gate, but make it local and fast.** The hook that refuses
   Write/Edit/Bash without an active issue becomes "refuse unless a Jira key
   (`CHAR-123`) is active". The active key lives in a local state file. The
   hook never calls Jira: hooks have a 3-second budget and can't reach the
   MCP anyway.
3. **Keep every `#NNN` meaning what it means today.** Chainlink IDs run
   contiguously from 1 to 439, and every parent has a lower number than its
   children. Import into a *brand-new* project, one issue at a time, in ID
   order, and **`#N` becomes `CHAR-N`**. The ~2,300 `#NNN` references in the
   repo, 301 commit messages and the memory files then stay readable without
   rewriting anything.
4. **Let the branch → staging → main flow move the tickets.** With GitHub for
   Jira installed and the key in branch and PR names, Jira Automation can
   handle status changes: PR opened → *In Review*, merged to `staging` → *On
   Staging*, promoted to `main` → *Done*. Then the Release Notes Drafter can
   write the CHANGELOG draft. That last part replaces chainlink's changelog
   writer, which writes backwards (it has happened 7 times).
5. **Handoff notes become a Jira comment on the issue you were working on**,
   and the session-start hook surfaces the latest one. Breadcrumbs
   (`session action`) are dropped: deciduous and Claude Code's own compaction
   already cover them.
6. **Hold off on Atlassian's coding agents** (Rovo Dev in Jira, Claude Agent
   for Jira). They're beta, and they run in a sandbox that skips our TDD,
   mutation-testing, deciduous and pre-push discipline. Do use the planning
   and triage AI (work breakdown, duplicate detection, Readiness Checker,
   natural-language JQL) if the site's plan includes Rovo.

---

## 2. What chainlink actually does today

I read the hooks and the chainlink database rather than going by the docs.

### 2a. Issue tracking (replace with Jira)

| Chainlink feature | Usage in this repo | Jira equivalent |
|---|---|---|
| Issues, priorities, labels | 439 issues (140 open, 299 closed) | Work items, Priority, Labels |
| Sub-issues | 67 | Epic → child, or Subtask |
| Comments | 424 | Comments |
| `relate` / `block` | 31 relations, 2 blockers | Issue links ("relates to", "is blocked by") |
| `issue ready` / `next` | shown at every session start | JQL filter (see §4c) |
| Milestones | 0 used | Fix versions (we'd actually start using them) |
| Timers | 0 entries | Worklogs (not needed) |
| `issue close` → CHANGELOG line | broken: writes it backwards | Release Notes Drafter / automation |

Labels have drifted (`bug`/`fix`, `perf`/`performance`,
`feature`/`enhancement`). The import is a free chance to normalise them.

### 2b. Ad-hoc discipline (keep, but take off chainlink)

| Mechanism | Where | Needs chainlink binary? |
|---|---|---|
| **Issue gate**: blocks Write/Edit/Bash with no active issue | `work-check.py` | Yes (`session status`). **Rewrite.** |
| **Git-mutation blocklist** (rebase, reset, tag, `branch -D`…) | `work-check.py` + `.chainlink/hook-config.json` | No. Config file only. |
| Bash allowlist (commands that skip the gate) | same | No |
| Session start: last handoff, ready + open issues, auto-end stale sessions, auto-comment on resume | `session-start.py` | Yes. **Rewrite.** |
| Handoff notes (80 sessions so far) | `chainlink session end --notes` | Yes. **Replace.** |
| Breadcrumbs for context compression | `session action` | Yes. **Drop.** |
| Coding-rules injection on every prompt (global, project, per-language rules; project tree; dependency list) | `prompt-guard.py` + `.chainlink/rules/*.md` | No. Reads files only. |
| Web-fetch rules + `safe-fetch` MCP | `pre-web-check.py`, `.claude/mcp/` | No |
| Stub/TODO detection, lint after edit, "run your tests" reminder | `post-edit-check.py` (uses a `.chainlink/` marker file) | Barely. `issue tested` resets the marker. |
| Pre-commit export + R2 backup of `issues.db` | `scripts/install-hooks.sh` | Yes. **Drop** (Jira is the system of record). |
| Token usage, locks, `cpitd` clone detection | chainlink subcommands | Unused here |

**⚠️ Template risk.** These hooks come from the project template
(`update-project-from-template`, last synced in PR #168). Editing them in
charcoal alone means the next template sync overwrites the changes. Either
make the change upstream in the template, with the tracker as a setting, or
have the template skip charcoal's tracker hooks. **This is the first decision
to make before any code** (§7, Q1).

---

## 3. Atlassian capabilities that matter (verified 2026-11-21)

Sources are in §8. "Plan" means the Jira plan on `makeworkbetter`, which I
couldn't read from the MCP. Check it in admin.atlassian.com.

| Capability | Plan | Use it? |
|---|---|---|
| **Remote MCP v2** (`mcp.atlassian.com/v2/mcp`): create/edit/transition/link/comment, JQL, worklogs, remote links. Deletes and project admin are off by default. | All, free to call | **Yes.** This is the main interface. |
| **GitHub for Jira** dev panel: key in branch/commit/PR title links it automatically; Actions builds + deployments shown | All | **Yes** |
| **Smart commits** (`CHAR-12 #comment …`, `#done`) | All. Admin must enable; committer email must match the Jira user | Optional. Automation is more reliable. |
| **Jira Automation** dev triggers (PR created/merged, branch, build, deployment) | Free: **150 steps/month total**. Standard: 400/user. Billed in *steps* from 2026-12-03 | **Yes**, but Free is tight (§4d) |
| **Fix versions + Releases hub** | All | **Yes**: one version per prod promotion |
| **Rovo AI**: work breakdown, duplicate detection, similar items, summaries, natural-language JQL | Standard+ (not Free). 25 credits/user/month on Standard | Yes, if available |
| **Rovo agents**: Readiness Checker, Release Notes Drafter, Triage, Blocker Checker | Standard+, uses credits | Release Notes Drafter + Readiness Checker are the good ones |
| **Rovo Dev in Jira / Claude Agent for Jira** (work item → sandbox → draft PR) | Standard+, beta. Claude Agent uses our own Anthropic key | **Not yet** (§5) |
| **CSV / JSON importer** | All | Possible, but see §6 for why REST is safer for key order |

The "claude.ai Atlassian Rovo" connector in your Claude settings still uses
the **deprecated v1 endpoint** (it will be removed 2027-03-01). The
"claude.ai Atlassian MCP" connector I used for this research is already on
v2. Remove the Rovo one, or re-add it against
`https://mcp.atlassian.com/v2/mcp`, so agents don't get two overlapping tool
sets.

---

## 4. Recommended target setup

### 4a. Project

- **Name / key:** `Charcoal` / **`CHAR`** (free on the site; `CHAR-439`
  reads naturally).
- **Type:** Jira **software, team-managed, Kanban**. Kanban because there are
  no sprints. Team-managed because you administer it yourself and it's
  simpler. If we later need shared schemes, components or advanced roadmaps,
  it can move to company-managed. (I made this call. Change it if you want
  `CHAR` to share setup with your other classic projects.)
- **Work types:** Epic (one per phase/initiative, which is what most chainlink
  parent issues already are), Task, Bug, Subtask. Add a **Spike** type for
  the `research`/`investigation` work, since those close with a report, not
  code.

### 4b. Workflow mapped to the existing promotion flow

```
To Do → In Progress → In Review → On Staging → Done
          (branch)     (PR open)   (merged to   (promoted to
                                    staging)     main / prod)
```

This makes the board answer "what's on staging but not prod?" on its own,
which today needs `git log staging..main`. Research and docs items with no
deploy go straight from In Progress to Done.

### 4c. Saved filters (to replace `ready` / `next`)

- **Ready:** `project = CHAR AND statusCategory = "To Do" AND issueLinkType
  != "is blocked by" ORDER BY priority DESC, created ASC`. Jira's JQL can't
  check whether the *blocker* is still open, so blocked items get the
  "Flagged" marker instead. With only 2 blockers in 439 issues, that's fine.
- **In flight:** `project = CHAR AND statusCategory = "In Progress"`
- **Staged, not shipped:** `project = CHAR AND status = "On Staging"`

### 4d. Automation rules (about 5 rules)

1. PR created whose title/branch has a key → *In Review*
2. PR merged into `staging` → *On Staging*
3. PR merged into `main` → *Done*, and add the item to the next unreleased fix
   version
4. Item → *Done* while it still has open subtasks → comment a warning
5. (Standard+) Version released → Release Notes Drafter → paste into
   `CHANGELOG.md` by PR (a human-reviewed step, not automatic)

**Cost check:** each rule run is roughly 3–4 steps. At the current pace
(about 2–4 PRs a day, each crossing two or three rules) that is
**~300–600 steps a month**. That is over Free's 150 and fits Standard's 400
only at the low end. If the site is on Free, keep just rule 3, and let the
agent move tickets via MCP as part of its normal flow.

### 4e. Railway deployments

Railway has no Jira integration. The cheapest way to get prod deploys into
the Jira Deployments view is a small GitHub Actions step that creates a
GitHub `deployment_status` when `main` updates. GitHub for Jira reads those.
Nice to have, not required.

---

## 5. Keeping the discipline: how the guardrails translate

### The issue gate (the important one)

Current behaviour: the hook calls `chainlink session status`. If there's no
"Working on: #N", it blocks with instructions.

Proposed:

- A local, gitignored state file, **`.jira/active`**, holds the key being
  worked on, plus who set it and when.
- `scripts/work CHAR-123` writes that file. The agent's flow is: create or find
  the item via MCP (`createJiraIssue` / JQL), then run `scripts/work CHAR-123`,
  which also moves it to *In Progress*.
- **Fallback:** if the current git branch name has a `CHAR-\d+` key in it,
  that counts as active. Sesh worktrees use fixed `sesh/...` branch names,
  which is why the state file comes first and the branch name is only a
  fallback.
- The block message changes to: "Create or find a Jira work item via the
  Atlassian MCP, then run `scripts/work CHAR-N`."
- The hook **never calls Jira**. That keeps it fast and working offline, so a
  Jira outage can't stop work. The trade-off is that it trusts the key exists.
  The commit and PR checks below catch typos.

### Backstops that Jira makes worthwhile

- **`commit-msg` git hook:** require a `CHAR-N` key in every commit message,
  or take it from `.jira/active` automatically. Atlassian has no enforcement
  app for this, so it's a plain git hook.
- **CI check:** the PR title must contain `CHAR-N`, and (optionally) that key
  must exist, via the Jira REST API with a read-only token stored as a GitHub
  secret. This is the "an issue exists for this work" guarantee, enforced at
  merge.

### Sessions and handoff notes

- **Session end** → the agent posts a comment on the active item, starting
  `Handoff:`, using the MCP. The handoff then lives with the work it
  describes, and you can read it from your phone in the Jira app.
- **Session start hook** injects the active key and tells the agent to read
  that item's latest `Handoff:` comment and the *Ready* filter through the
  MCP. Optionally, if a `JIRA_API_TOKEN` is set (sesh sessions have 1Password
  available; Safehouse ones don't), the hook fetches them itself with `curl`
  and fails quietly if it can't.
- **Stale-session auto-end** and **breadcrumbs:** dropped. Deciduous already
  records actions in real time, and Claude Code's compaction makes
  `session action` redundant.

### Guardrails that aren't about issues

Move `.chainlink/rules/` → `.claude/rules/` and `hook-config.json` →
`.claude/hook-config.json`. Update the four hooks' path lookups. The git
blocklist, rules injection, web rules, safe-fetch and stub/lint checks keep
working exactly as now. The "tests were run" marker moves to
`.claude/.cache/`.

### Why not Atlassian's coding agents yet

Rovo Dev in Jira and Claude Agent for Jira both turn a work item into a draft
PR from a cloud sandbox. They never merge, which is good. But they don't load
our hooks, so the tests-first rule, deciduous logging, mutation testing and
model-gated test checks don't apply. Both are beta and cost credits. Worth a
trial later on a docs-only item (for example one of the #126–#130 help pages).

---

## 6. Migration plan

**Goal:** `#N` → `CHAR-N` for every issue, closed ones included.

Why this is possible: the IDs are 1–439 with no gaps, and every sub-issue's
parent has a lower ID. So creating items in ID order always creates the
parent first.

1. **Freeze.** End the chainlink session, take a final export, and back
   `issues.db` up to R2 one last time under `history/`.
2. **Dry run in a scratch project** (`CHARX`). Run a small Python script
   against the Jira REST API that creates issues **one at a time in ID
   order**, not the 50-per-request bulk endpoint, whose ordering isn't
   guaranteed. Check that key N matches #N for all 439. One failed create
   leaves a permanent gap, because Jira never reuses keys. The script
   therefore stops on the first mismatch, and the fix is to delete the
   scratch project and start again.
3. **For each issue, carry over:**
   - title
   - description, converted from Markdown to Jira's format
   - priority
   - normalised labels
   - a `chainlink-id` label or the External ID field
   - the parent
   - the original created and closed dates, as a footer line in the
     description

   REST can't backdate "Created". Comments go in as one comment each, each
   prefixed with its original timestamp. Closed issues are transitioned to
   Done.
4. **Second pass for links:** the 31 relations and 2 blockers.
5. **Verify:** counts per status and label match, 20 random spot-checks, and
   `CHAR-N` title == `#N` title for all N.
6. **Run it for real in `CHAR`**, then verify again.
7. **Cut over** in a single PR:
   - swap the hooks (§5) and the pre-commit export/backup steps
   - update `CLAUDE.md`, `docs/deciduous-workflow.md` and the memory
     entries ("No GitHub issues" → "Jira is the only tracker; `#N` ==
     `CHAR-N`")
   - remove `chainlink` from `scripts/check-updates.sh`
8. **Leave the old `#N` references alone.** Add one line to CLAUDE.md:
   "historical `#N` = `CHAR-N`". New work uses `CHAR-N` everywhere.

Rough effort: the import script plus dry run is about half a day. The hook
rewrite plus template coordination is about a day, with property tests on the
key-parsing logic per the testing rules.

The alternative I rejected: the **CSV/JSON importer** can backdate "Created"
and set all fields at once. But whether it keeps creation order, and so
whether key N = #N, isn't documented. Losing the 1:1 mapping costs far more
than losing exact creation dates. It could still be tried in the scratch
project first, if exact dates matter to you.

---

## 7. Decisions for you

1. **Template:** change the template upstream so the tracker is a setting,
   or fork charcoal's hooks off the template? *(Recommend upstream: your
   other projects will want the same thing.)*
2. **Plan level of `makeworkbetter`:** Free means no Rovo AI and 150
   automation steps a month. Standard or higher unlocks §3's AI rows. (I
   couldn't read this from the MCP.)
3. **Project key `CHAR`** and team-managed Kanban: OK?
4. **Import closed issues too?** *(Recommend yes. It's required for the
   `#N` = `CHAR-N` mapping.)*

## 8. Sources

- MCP v2 tools / auth / upgrade:
  developer.atlassian.com/cloud/rovo-mcp/guides/supported-tools/ ·
  …/authentication-and-authorization/ ·
  support.atlassian.com/atlassian-ai-gateway/docs/how-to-upgrade-from-atlassian-mcp-v1-to-atlassian-mcp-v2/
- MCP enhancements (write-back, dev activity), 2026-07: atlassian.com/blog/jira/mcp-enhancements
- Rovo credits: support.atlassian.com/rovo/docs/rovo-usage-limits/
- Atlassian agents: support.atlassian.com/rovo/docs/atlassian-agents/
- Rovo Dev in Jira: support.atlassian.com/rovo/docs/work-with-rovo-dev-in-jira/
- Claude Agent for Jira (beta): support.atlassian.com/jira-software-cloud/docs/set-up-claude-agent-for-jira/
- Automation step billing: support.atlassian.com/cloud-automation/docs/how-is-my-usage-calculated/
- Dev-tool automation triggers: atlassian.com/software/jira/automation-template-library/bitbucket-github-gitlab
- Linking dev work / smart commits:
  support.atlassian.com/jira-software-cloud/docs/reference-issues-in-your-development-work/ ·
  …/process-issues-with-smart-commits/
- GitHub deployments in Jira: support.atlassian.com/jira-cloud-administration/docs/link-github-workflows-and-deployments-to-jira-issues/
- CSV mapping: support.atlassian.com/jira-software-cloud/docs/mapping-csv-data-to-jira-fields/
- Bulk create (50 max): developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-bulk-operations/
- Pricing: atlassian.com/software/jira/pricing

**Not verified:** MCP rate limits (community reports say ~500–1,000 calls an
hour, and lower with API tokens; Atlassian doesn't publish them). Whether the
MCP fully works on a Free site. Per-agent credit costs. Whether team-managed
projects have components.
