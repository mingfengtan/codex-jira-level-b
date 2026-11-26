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

For unexpected CLI, Git, GitHub, test, or Jira errors, the same saved stage can be retried with capped exponential backoff. Failed test runs feed their output into the same Codex session for up to `MAX_TEST_REPAIRS` additional attempts. After `MAX_RETRIES` consecutive failures, a clearly unsafe operation, a removed authorization label, or a Git/PR conflict, the local stage becomes `manual`; the worker tries to move Jira to `Blocked` and leaves the files intact. Fix the underlying problem before manually reauthorizing a retry.

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
