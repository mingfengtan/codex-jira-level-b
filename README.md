# Codex + Jira (Level B) — Flutter physics-puzzle game starter

This repository is the **orchestrator**, not the game code. `REPO_PATH` in `.env` points to your **application** Git checkout. It has a local `main` commit and a GitHub `origin` remote, and you must add `AGENTS.md` to that repo before the worker starts.

## What's included

- `project_brief.md`: complete product requirements and phase boundaries (P0/P1 now; P2/P3/P4 roadmap only).
- `plan.schema.yaml`: authoritative YAML-form JSON Schema for local validation and Codex structured-output conversion.
- `plan.example.game.yaml`: **illustrative** P0/P1 Epics/Stories/Tasks and dependency graph. It is **not applied by default**. Replace its `project` before applying if your Jira key differs.
- `orchestrator.py`: generate a YAML plan, validate, sync Jira issues, implement one approved ticket, or combine steps in a scheduled `tick`. Execution jobs now survive quota exhaustion, exceptions, and many process restarts using durable checkpoints.
- `AGENTS.example.md`: copy to application repository root as `AGENTS.md` and commit it to `main`.
- `tests/`: isolated unit tests; no real Jira or GitHub credentials needed.

## Setup (Python 3.11+, Linux or macOS)

1. Configure and authenticate `codex`, `git`, and `gh` on the host that runs the worker. `gh auth status`; `gh auth setup-git`; `codex login`; `flutter doctor -v`.
2. You need Flutter installed locally and enabled for Android/iOS/macOS where supported. Native iOS/macOS builds require a Mac with Xcode. To generate tickets only, the Flutter SDK is not yet required, but coding tasks require it.
3. Clone the private GitHub app repository locally, push an initial `main` commit. Copy `AGENTS.example.md` to `<app-repo>/AGENTS.md`, commit and push.
4. `cp .env.example .env`; edit `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `REPO_PATH` and `CHECK_COMMAND`. Existing `.env` can be **retained** when upgrading.
5. `python3 -m pip install -r requirements.txt` (prefer a Python virtual environment). Do not commit `.env`.
6. In Jira use your Team-managed Kanban workflow with `Ready`, `In Progress`, `In Review`, `Blocked`, `Done`. Allow transitions; ensure at least Epic, Story, Task and Subtask work types exist.
7. `CHECK_COMMAND=flutter pub get && flutter analyze && flutter test` is a reasonable baseline. For specific device integration/performance tests, run separate CI/device jobs; the default command is not proof of iOS/macOS builds.

> The Python `flock` lock is for a **single Linux/macOS host**, not distributed coordination. The Python worker deliberately passes the Jira token only to the controller process, not to Codex or project test subprocesses. However, untrusted repository scripts still pose execution risks; use a disposable runner for untrusted code.

## Increasing automatic review fix rounds

The `.env` setting `MAX_AUTO_REVIEW_FIXES` is read on each scheduler invocation.
If an existing PR reached `manual` **specifically** because its prior automatic
review-fix budget was exhausted, increasing this setting beyond its saved
`fix_rounds` count now automatically resumes that PR on the next
`review-run` or `review-once`. It uses Agent B's saved `changes_requested`
findings and preserves the review/fix history. No checkpoint editing,
`review-retry`, or `review-resume` is necessary for this case.

For example, if `SSG-8` has `fixes=2`, set `MAX_AUTO_REVIEW_FIXES=4` in
**`.env`** (not just `.env.example`) and execute:

```bash
python3 orchestrator.py review-run --issue-key SSG-8 --dry-run
python3 orchestrator.py review-run --issue-key SSG-8
```

`review-status` shows `fixes=2/4` and a `raise-limit recovery available`
marker while that old checkpoint is eligible to resume. Only an exact budget-exhausted
stop with a saved Agent B review result can automatically recover. Other
manual stops still require investigation. The PR remains human-merge-only.

**Config precedence:** the scheduler's `.env` loader uses `setdefault`, so an
already exported shell environment variable overrides `.env`. To check the
actual value read by the scheduler:

```bash
python3 -c 'import orchestrator, review_worker; orchestrator.load_env(); print(review_worker.config()["max_fixes"])'
```

## Modes: choose one

### A. Review plan locally before creating Jira issues (safer first run)

```bash
python3 orchestrator.py plan project_brief.md --out plan.yaml
python3 orchestrator.py validate-plan plan.yaml
# Review plan.yaml, edit if necessary, and approve the choice to create issues:
python3 orchestrator.py apply-plan plan.yaml --yes
```

### Live planning progress, logs, and Jira project key

The `plan` command streams `codex exec --json` events to `logs/plan-generation.jsonl`
while displaying session starts, turn status, tool activity and a heartbeat every
15 seconds (configurable with `PLAN_HEARTBEAT_SECONDS`). The heartbeat means
**the child process is still running**, not necessarily that the model is
making forward progress. The per-run JSONL log may include project context and
should remain private and uncommitted.

```bash
# Scheduler directory, not app directory:
python3 orchestrator.py plan project_brief.md --out plan.yaml \
  --log logs/plan-generation.jsonl
# In another terminal:
tail -f logs/plan-generation.jsonl
```

After Codex exits successfully, its raw response is retained as
`logs/plan-generation.last.json` **before** semantic validation, so a failed
validation does not discard a lengthy planning result. Do not commit or share
this file without checking its content. The final `plan.yaml` is written only
after validation passes. `Ctrl+C` stops the planning subprocess; unlike ticket
execution, planning session resume is not yet implemented.

The project key is **owned by `.env`** (`JIRA_PROJECT_KEY=SSG` in this setup).
During generation the scheduler constrains the temporary Codex output schema
to that exact value. If Codex nevertheless returns a different value, only the
generated `project` field is normalized and a warning is printed. User-supplied
YAML remains strictly checked: `validate-plan` and `apply-plan` refuse any
plan whose `project` does not match `.env`. No Jira tickets are created by
`plan`.

### B. Automatically create Jira tickets from the brief (requested mode)

```bash
python3 orchestrator.py bootstrap --brief project_brief.md --plan plan.yaml --yes
```

If `plan.yaml` does not exist, Codex generates it from the brief (P0/P1 only). If it exists, it is reused. `bootstrap` then creates/reconciles Epics, Stories, Tasks, optional Subtasks and `Blocks` links. It writes `plan.applied.json` as a local mapping. **No new ticket is automatically authorized**: it uses `codex-planned`, not `codex-ready`.

To run bootstrap and one approved coding task in one scheduled tick:

```bash
python3 orchestrator.py tick --bootstrap --brief project_brief.md --plan plan.yaml
```

The `--bootstrap` flag explicitly opts into automatic Jira ticket creation on each scheduled tick. Running `tick` without it only processes existing eligible tickets. Use `--dry-run` to inspect coding selection without writing anything (dry-run **never bootstraps**):

```bash
python3 orchestrator.py tick --dry-run
```

**Important:** `--bootstrap` will also sync *new issue IDs* added to an edited `plan.yaml` on later runs. Keep the plan backed up, review edits, and retain `plan.applied.json` when deploying or changing machines. Summary/description edits to existing IDs are **not** synced to Jira after creation. The state file reduces duplicates but cannot guarantee exactly-once Jira creation across a crash between Jira's successful response and saving local state.

## Review gate and runtime

1. Review created Jira issues. Select the *first unblocked small task* (e.g. Flutter bootstrap). Set **status `Ready` AND label `codex-ready`**. Where necessary, manually set Jira priority: suggested priorities in the YAML are written into issue descriptions, not Jira priority fields (which are site-configurable).
2. Run `python3 orchestrator.py once --dry-run` to verify selection.
3. Run `python3 orchestrator.py once` to execute once. Or schedule `tick` every 30 minutes as shown below.
4. The worker fetches `origin/main`, creates a new `codex/KEY` worktree, runs Codex with workspace-write sandbox, then executes `CHECK_COMMAND` **on the host**, commits with the Jira key, pushes and opens a **draft PR**. It comments with the PR URL and transitions Jira to `In Review`.

**Worker progress and diagnostics:** As of this version, `once` prints each execution stage (`prepare`, `codex`, `verify`, `commit`, `push`, `pr`, `jira`, `cleanup`), Codex session/turn events, and an approximately 15-second Codex heartbeat with the number of events and time since the last event. This deliberately avoids printing model text or private source content to the terminal. The raw local Codex event stream remains in `~/.codex-jira-worktrees/logs/SSG-2.log` (substitute your Jira issue key); inspect with `tail -f`. An active process does **not** necessarily mean model progress.

If `once` is already running under an older scheduler, **do not replace the Python file and start another worker**. Observe its saved state with `python3 orchestrator.py status` and its live log with `tail -f ~/.codex-jira-worktrees/logs/SSG-2.log`. Let that invocation complete, or stop/recover it deliberately, before using the updated worker. To inspect implementation changes, run `git -C ~/.codex-jira-worktrees/SSG-2 status --short`; changes do not appear in your main `ssg` checkout until the PR is merged and pulled.

5. A human reviews/merges the draft PR and moves Jira ticket to `Done`. Only then should downstream tickets be approved/unblocked. Codex does not auto-merge.
6. A parent Story/Task with Subtasks will never be executed by the worker; execute only its child Subtasks and close the parent manually after they complete. New Subtasks remain unapproved.

Example cron (use full Python path and ensure Codex, Flutter, Git and gh are on cron's PATH):

```cron
*/30 * * * * cd /ABSOLUTE/PATH/codex-jira-level-b && /ABS/PYTHON/bin/python orchestrator.py tick --bootstrap >> /ABS/PATH/codex-jira.log 2>&1
```

This requires an online host. Turn off cron or omit `--bootstrap` if you want to freeze Jira ticket creation after the first batch. Start scheduling **only after the first manual end-to-end PR succeeds**.

## Schema, Git, and safety notes

- `plan.schema.yaml` is parsed using PyYAML's safe loader and validated with `jsonschema`. For `codex exec --output-schema`, the scheduler **recursively creates a strict version in a temporary JSON file** (all nested properties required and `additionalProperties: false`). The editable YAML remains the only maintained schema and can still treat `phase`, `type`, `priority`, and `subtasks` as optional for hand-written plans. The model-generated plan will provide those fields (e.g. `subtasks: []`).
- `plan.example.game.yaml` contains a sample `project: AIDEV`: replace it with your actual `JIRA_PROJECT_KEY`. If using it instead of AI generation, copy to `plan.yaml`, validate, then bootstrap/apply.
- Plan `type` can be `Task`, `Story` or `Bug`. Nested `subtasks` are created using Jira's actual subtask type ID. `priority` is a human-facing hint; change priority in Jira for queue ordering. Dependencies refer to stable plan IDs. Don't create dependency cycles.
- Codex is instructed to avoid commits/pushes/Jira calls; the orchestrator owns remote operations. Protect the GitHub `main` branch with mandatory PR review and CI checks; approve and merge manually. Native Jira coding-agent triggers should not run on the same issues.
- Quota failures keep the ticket `In Progress`, preserve worktree/session, and pause further Codex tickets until the cooldown expires. Ordinary errors receive bounded retries with backoff; unrecoverable/ambiguous conditions move into local `manual` state and the worker attempts to mark the Jira ticket `Blocked`. Existing worktrees are never automatically deleted after a failure.
- The worker has automatic recovery (details below), but cannot guarantee recovery from every possible hardware failure, auth revocation, corrupted repository, or untracked external side effect. The local lock does not prevent workers on different hosts. There is no automatic PR merge detection.
- A large feature, commercial payments, ads, internet downloads or platform releases should be decomposed into explicitly approved new phases—not smuggled into the initial plan.


## Upgrade from the pre-recovery MVP (important)

1. Disable your old cron/scheduled invocation temporarily. Back up the orchestrator directory **and** `WORKTREE_ROOT` (default `~/.codex-jira-worktrees`).
2. Copy the new `orchestrator.py`, `README.md`, and tests from this ZIP into the existing orchestrator directory. Keep the existing `.env`, `project_brief.md`, `plan.yaml`, `plan.applied.json`, and app repository unchanged. You may add the optional retry variables shown in `.env.example`.
3. Run `python3 -m pip install -r requirements.txt`, `python3 -m unittest discover -s tests -v`, then `python3 orchestrator.py status`.
4. Test `python3 orchestrator.py once --dry-run` and `python3 orchestrator.py once` before re-enabling cron. Do not create a second worker on another machine pointing to the same repo/state root.

**Recover an interrupted ticket from the old MVP:** The old version does not have a JSON checkpoint, so the upgraded scheduler cannot know the saved Codex thread. If an old worktree remains at `~/.codex-jira-worktrees/AIDEV-123` (use your own issue key), retain the worktree, branch and partial source. Keep the `codex-ready` label on the ticket. Explicitly adopt it with:

```bash
python3 orchestrator.py recover AIDEV-123 --adopt-worktree --retry-now
```

The adoption verifies the branch and starts a new Codex session instructed to inspect and continue the existing files; it never resets the checkout. If Jira currently shows `Blocked`, the command attempts a transition to `In Progress`. If the branch/worktree does not match, it refuses to proceed. Always inspect the worktree first.

## Quota-aware pause and restart recovery

The worker stores a restricted-permission JSON journal at:

```text
~/.codex-jira-worktrees/
├── .worker.lock
├── state/
│   └── AIDEV-123.json        # stage, Codex session ID, retries, PR URL, timestamps
├── logs/
│   ├── AIDEV-123.log         # Codex event stream and test logs
│   └── AIDEV-123.last.md
└── AIDEV-123/                # isolated Git worktree until a PR is created
```

The worktree and journal **must be on persistent local storage**; don't place them in an automatically cleaned temporary directory. Never commit the state directory, credentials or logs. A single-host `flock` prevents overlapping scheduled invocations. The `codex` CLI's JSON event stream supplies an exact session ID for `codex exec resume <SESSION_ID>`; the worker does not use `--last` (which could continue another ticket).

A retrying job is prioritized over new Ready tickets. If the failure is a **Codex quota limit**, the worker keeps the job in `codex` stage, leaves Jira `In Progress`, posts one quota comment, and waits `QUOTA_RETRY_SECONDS` (default 1800). When the scheduler next runs after the cooldown, it tries to resume the saved Codex session. Quota classification uses CLI error text and therefore is best-effort; the worker cannot directly query a precise quota reset timestamp. All newly selected tickets are paused while a quota-paused job exists.

For unexpected CLI, Git, GitHub or Jira errors, the same saved stage can be retried with capped exponential backoff. **Application verification failures are different:** the worker immediately feeds the failed command's output back into the existing Codex session, reruns verification, and continues automatically for up to `MAX_TEST_REPAIRS` *additional* Codex repair attempts in the same `once`/`tick` invocation. It never commits or pushes changes while verification is failing. After the test-repair budget is exhausted, `MAX_RETRIES` consecutive infrastructure failures, a clearly unsafe operation, a removed authorization label, or a Git/PR conflict, the local stage becomes `manual`; the worker tries to move Jira to `Blocked` and leaves the files intact. Raising `MAX_TEST_REPAIRS` can resume a checkpoint stopped *specifically* due to the test-repair budget on the next `once`. See `UPGRADE-AUTO-VERIFY-REPAIR.md`.

```bash
python3 orchestrator.py status
python3 orchestrator.py once --dry-run
python3 orchestrator.py once

# After fixing an error, override the cooldown / manual stop explicitly:
python3 orchestrator.py recover AIDEV-123 --retry-now
```

The scheduler will replay Git push, PR lookup/creation, Jira comments/transitions, and worktree cleanup when needed. It first checks whether an open PR already exists and whether the Jira comment marker was posted, reducing duplicates after crashes. If a PR has been closed/merged unexpectedly, it requests manual review instead of silently creating another. It refuses to erase worktrees or overwrite conflicting branches.

**Process restarts:** SIGINT/SIGTERM stop supervised Codex/test subprocess groups and keep the checkpoint. After an abrupt kill/power failure, the next run detects any still-alive previous process group and avoids launching a duplicate. If a process is alive or its identity is uncertain, inspect it before retrying. Hardware loss, broken Git state, lost Codex session records, missing credentials, or ambiguous external operations may still require human intervention. Journaling is local, not a distributed job queue.

**Scope:** This recovery system covers **ticket execution**. An interrupted initial `plan`/`bootstrap` generation is rerun at the next scheduled invocation (not a saved Codex planning session). Jira ticket creation retains the prior `plan.applied.json` mapping and its earlier small create-vs-save duplication window. Do not schedule `--bootstrap` unless you want ongoing plan synchronization.

## Validation and tests

```bash
python3 -m unittest discover -s tests -v
python3 orchestrator.py validate-plan plan.example.game.yaml
python3 orchestrator.py --help
```

Run `flutter doctor -v` and platform smoke tests separately; the Python tests do not create tickets, run Codex or exercise a real Flutter project.


## Codex CLI executable and approval flag (macOS)

If `codex` in a Python virtual environment starts an unrelated app, set
`OPENAI_CODEX_BIN=/opt/homebrew/bin/codex` in the **orchestrator** `.env`.
Verify with `/opt/homebrew/bin/codex --version` (expected: `codex-cli ...`).
This release also removes `--ask-for-approval never` from `codex exec`
because Codex CLI 0.172.0 rejects that argument when placed after `exec`.
Noninteractive `exec` uses noninteractive approval behavior and the configured
`--sandbox` restriction; do not use the dangerous bypass flags.


## Structured output schema compatibility (Codex CLI)

If Codex reports `Invalid schema for response_format 'codex_output_schema'`
with `Missing 'phase'`, the previous scheduler forwarded the YAML schema
unchanged to the strict API. This version automatically converts all nested
object properties to required **only for the temporary Codex output schema**,
leaving `plan.schema.yaml` and local plan validation unchanged. Replace
`orchestrator.py`, then rerun `python3 orchestrator.py plan project_brief.md --out plan.yaml`.
This error occurs before a plan is generated and does not create Jira tickets.

Confirm the update with:

```bash
python3 -m unittest discover -s tests -v
```

## Fix wrong-direction Jira blockers (dependency hotfix)

Earlier versions of this MVP created `Blocks` links with the Jira REST API's
inward/outward ends reversed. The YAML plan itself was correct. Existing links
**must be audited and explicitly repaired**; replacing the script alone does not
repair them because `plan.applied.json` already marks the pair applied.

From your scheduler directory, **with your existing `.env` and `plan.applied.json`**:

```bash
python3 orchestrator.py audit-dependencies plan.yaml
```

Read the `REVERSED`, `MISSING`, and `BOTH` lines. If the mapping looks right,
stop any scheduled workers, back up `plan.applied.json`, and run:

```bash
python3 orchestrator.py audit-dependencies plan.yaml --fix --yes
python3 orchestrator.py audit-dependencies plan.yaml
python3 orchestrator.py once --dry-run
```

Repair modifies **only the blocker pairs explicitly listed by `depends_on` in
this plan**. It deletes reversed Jira `Blocks` links for these pairs and adds
missing correct links. It leaves all unrelated Jira links untouched. It fetches
link IDs from Jira before deletion, and a repeated repair is idempotent.

**Do not delete `plan.applied.json` or re-run `apply-plan` to repair existing
links**: that can lose the stable mapping and create duplicate Jira issues.

The standard Jira `Blocks` link type is assumed to use the English link name
`Blocks`. If your site customizes that link type's name or directions, adjust
and verify the link semantics before using `--fix`.

## Optional independent Agent B review / Agent A fix loop

This version adds `review_worker.py` and is **opt-in**. It does not change the
existing `plan`, `apply-plan`, `once`, or human-only PR merge workflow. First
complete one ordinary ticket and draft PR end to end.

In the **scheduler** `.env` (not the app repository), add:

```dotenv
AUTO_REVIEW_ENABLED=true
CODEX_REVIEW_MODEL=
CODEX_REVIEW_EFFORT=medium
CODEX_FIX_MODEL=
CODEX_FIX_EFFORT=medium
MAX_AUTO_REVIEW_FIXES=2
REVIEW_RETRY_SECONDS=600
REVIEW_REQUIRE_CI=true
```

`CODEX_REVIEW_MODEL` and `CODEX_FIX_MODEL` are optional Codex CLI model IDs.
Leave blank for the Codex CLI default; set different **supported** model IDs to
make the two agents more independent. The two agents always use separate Codex
sessions, and Agent B uses a read-only worktree. All PR writes are performed by
the orchestrator with `gh`; the agents receive no GitHub or Jira credentials.

Try read-only selection first:

```bash
python3 orchestrator.py review-once --dry-run
```

Then process one review operation:

```bash
python3 orchestrator.py review-once
python3 orchestrator.py review-status
```

Or let the existing scheduler run one coding ticket plus at most one review/fix
operation on each scheduled invocation:

```bash
python3 orchestrator.py tick --review
```

The review worker picks **completed coding jobs that have an open draft PR and
whose Jira issue is still `In Review`**. It uses the committed PR head SHA for
review identity, asks Agent B to inspect the diff and return structured findings,
then posts GitHub `COMMENT` review notes (with inline comments only where the
specified path/line exists in the current PR diff). If GitHub rejects review
submission with HTTP 422 (for example, self-review restrictions), the worker
posts the findings as an ordinary PR conversation comment instead. It does not
approve or request changes formally, because GitHub accounts cannot approve
their own PRs and this automated review is advisory.

On blocking findings, Agent A fixes the same PR branch in an isolated worktree,
runs `CHECK_COMMAND`, commits using the Jira key, and pushes the fix without
force-pushing. The next scheduler invocation re-reviews the new commit.
`MAX_AUTO_REVIEW_FIXES` limits this loop to prevent uncontrolled quota usage.
After a passing review and required green GitHub checks, the scheduler adds a
Jira comment and records `passed`; the PR still needs **human approval and
merge**. It never automatically marks Jira Done. `passed` is tied to a specific
PR SHA, so a subsequent push triggers a fresh review on the next invocation.
If no CI checks exist, `REVIEW_REQUIRE_CI=true` keeps the review in a pending
state; configure CI or explicitly set the option to `false` for a test setup.

The worker saves checkpoints in
`~/.codex-jira-worktrees/review/<ISSUE>.json`, separate read-only/fix worktrees
under `review/worktrees/`, and logs in `review/logs/`. Preserve these during
upgrades. When a review is temporarily paused, run `review-status` and review
logs. `review-retry ISSUE-123` clears a retry cooldown but does **not** override
manual intervention; inspect unsafe failures rather than repeatedly retrying.
For a production deployment, provision a distinct GitHub App/bot for Agent B
if you need an independently accountable review identity.

### Hyperlinks in GitHub and Jira

For newly created draft PRs, the PR description now starts with a Markdown link
such as `[SSG-4](https://YOUR_SITE.atlassian.net/browse/SSG-4)` instead of an
unlinked Jira ticket number. For **new Jira comments** about draft PRs or AI
review results, the worker builds Jira Cloud ADF `link` marks, making the PR
URL clickable inside Jira. Existing PR descriptions and existing Jira comments
are not overwritten or re-posted just to change formatting.

### Safety and current limits

- The code has **offline mocked tests** but has not been executed against your
  private GitHub and Jira accounts. Start with one expendable test PR.
- Human review and merge remain mandatory; protect the `main` branch.
- Review notes and model responses can be wrong. Diff-relative inline locations
  are validated, but some findings may be relegated to the review summary.
- Use the local GitHub CLI identity and repository permissions carefully. If
  the PR author and review publisher are the same account, GitHub may reject a
  formal review; the worker falls back to a normal PR comment.
- Repeatedly failing tests, unexpected branch updates, or permissions issues
  pause for manual intervention; they are **not** auto-merged or force-pushed.
- Logs and saved comments contain proprietary code context. Keep the scheduler
  state and `.env` out of version control.


### Auto-repair failing checks during AI review fixes

The review worker now distinguishes **failed Dart/Flutter tests or analysis**
from **environment/configuration failures** while verifying Agent A's review
fixes. The latter are sent to human intervention without consuming model quota.
For recognizable application failures it writes a restricted log to:

```text
~/.codex-jira-worktrees/review/logs/SSG-8-fix-verify.log
```

Then it checkpoints `fix_test_repair` with the failure output. The next
`review-once` or `tick --review` invocation resumes **the same Agent A Codex
thread** using its stored session ID and asks for a focused repair. A later
invocation re-runs verification. On success, the scheduler continues to
commit, push, and re-review on the same GitHub PR.

```dotenv
MAX_REVIEW_TEST_REPAIRS=2
```

`MAX_REVIEW_TEST_REPAIRS` is the number of *additional repair attempts per
review-fix round*, separate from `MAX_AUTO_REVIEW_FIXES` (how many cycles of
Agent B findings -> Agent A fixes are allowed). Repeated failures stop at
`manual` rather than retrying tests indefinitely. Unknown failures are also
left for human investigation. **No merging or Jira Done transition is added.**

Upgrade for a checkpoint already paused at `fix_verify` (such as SSG-8):
stop the scheduler, back up the scheduler and review checkpoint directory,
replace `review_worker.py` with the patched file, and run:

```bash
python3 orchestrator.py review-status
python3 orchestrator.py review-retry SSG-8
python3 orchestrator.py review-once
python3 orchestrator.py review-status
```

If test failures are recognized, expect `fix_test_repair` in the status output.
Run `review-once` again to let Agent A perform one repair, then again for host
verification. Monitor the detailed verification output with:

```bash
tail -f ~/.codex-jira-worktrees/review/logs/SSG-8-fix-verify.log
```

The successful task still requires GitHub CI, AI re-review, and human PR review
before merging. If the checkpoint is already `manual`, this recovery procedure
cannot restart it automatically: inspect the cause and checkpoint first.


### Continuous PR review runner (`review-run`)

The original `review-once` and `tick --review` still work as one-shot,
checkpoint-by-checkpoint commands. To avoid manually advancing `recheck`,
`review`, `fix`, verification, and `ci` stages, use **`review-run`** instead:

```bash
# Runs one eligible PR review end-to-end, or until a real wait/manual stop.
python3 orchestrator.py review-run

# Target a particular ticket; checks PR HEAD / CI immediately when waiting.
python3 orchestrator.py review-run --issue-key SSG-8

# Short bounded interactive run (saves state if it must exit).
python3 orchestrator.py review-run --issue-key SSG-8 --max-minutes 30

# Preview selection; no Codex calls.
python3 orchestrator.py review-run --dry-run
python3 orchestrator.py review-status
```

Required setting: `AUTO_REVIEW_ENABLED=true`. Configurable polling defaults:

```dotenv
REVIEW_RUN_MAX_MINUTES=90
REVIEW_HEAD_POLL_SECONDS=3
REVIEW_HEAD_WAIT_TIMEOUT=120
REVIEW_CI_POLL_SECONDS=15
REVIEW_CI_WAIT_TIMEOUT=900
```

On each invocation, the runner picks **one PR** and uses the existing
saved JSON checkpoint. It automatically advances through Agent B's review,
Agent A's bounded fixes, test-failure repairs, same-branch commit/push,
PR-head synchronization, another review, and GitHub CI. A successful HEAD
or CI status is acted upon immediately, without waiting for the legacy
`REVIEW_RETRY_SECONDS=600` backoff. If still pending, it polls at the configured
interval up to the configured bound; then returns cleanly with a checkpoint.
Reinvoke `review-run` (or schedule it) to resume. Transient failures still
honor the existing retry backoff, and quota pauses are *not* bypassed.

`--max-minutes` is a **soft** time budget checked between stages; an
already-running Codex or Flutter subprocess can outlast it. The worker
uses an exclusive local lock; do not run multiple schedulers concurrently.
It never automatically approves or merges a PR or marks Jira Done.

**When the review-fix limit is exhausted:** The checkpoint becomes `manual`
and the runner stops: `Automatic review fix budget exhausted`. Inspect the
last review's summary/inline comments (and the review JSON checkpoint).
Do not use `review-retry` to bypass this manual stop. A human may decide
whether the remaining findings are valid and manually fix/push a new commit.
Only then, explicitly authorize an independent re-review:

```bash
python3 orchestrator.py review-resume SSG-8
python3 orchestrator.py review-run --issue-key SSG-8
```

`review-resume` works **only** for a budget-exhausted manual checkpoint and
**only** after the GitHub PR HEAD SHA changed. It does not reset the saved
fix counters and cannot give Agent A another automatic fix round. If Agent B
still requests changes, it returns to human intervention. Other manual stops
(e.g., failed CI, unknown environment error, unexpected branch change) require
investigation; they are not bypassed by this command. Preserve all existing
`~/.codex-jira-worktrees/review/` checkpoints and worktrees during upgrade.

To install, replace **both** `orchestrator.py` and `review_worker.py` with the
matching updated versions while the scheduler is stopped, after backing up
both files. Keep `.env` and saved state files unchanged; add the optional
polling settings if desired.
