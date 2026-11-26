# Codex + Jira (Level B) — Flutter physics-puzzle game starter

This repository is the **orchestrator**, not the game code. `REPO_PATH` in `.env` points to your **application** Git checkout. It has a local `main` commit and a GitHub `origin` remote, and you must add `AGENTS.md` to that repo before the worker starts.

## What's included

- `project_brief.md`: complete product requirements and phase boundaries (P0/P1 now; P2/P3/P4 roadmap only).
- `plan.schema.yaml`: authoritative YAML-form JSON Schema for local validation and Codex structured-output conversion.
- `plan.example.game.yaml`: **illustrative** P0/P1 Epics/Stories/Tasks and dependency graph. It is **not applied by default**. Replace its `project` before applying if your Jira key differs.
- `orchestrator.py`: generate a YAML plan, validate, sync Jira issues, implement one approved ticket, or combine steps in a scheduled `tick`.
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

- `plan.schema.yaml` is parsed using PyYAML's safe loader, validated with `jsonschema`, and converted temporarily to JSON because `codex exec --output-schema` accepts a JSON Schema file. You maintain **one schema YAML**.
- `plan.example.game.yaml` contains a sample `project: AIDEV`: replace it with your actual `JIRA_PROJECT_KEY`. If using it instead of AI generation, copy to `plan.yaml`, validate, then bootstrap/apply.
- Plan `type` can be `Task`, `Story` or `Bug`. Nested `subtasks` are created using Jira's actual subtask type ID. `priority` is a human-facing hint; change priority in Jira for queue ordering. Dependencies refer to stable plan IDs. Don't create dependency cycles.
- Codex is instructed to avoid commits/pushes/Jira calls; the orchestrator owns remote operations. Protect the GitHub `main` branch with mandatory PR review and CI checks; approve and merge manually. Native Jira coding-agent triggers should not run on the same issues.
- Failure transitions ticket to `Blocked`, logs the error and leaves any worktree for investigation. The local lock does not prevent duplicate workers on other hosts. After fixing a failed run, investigate worktree/branch/PR state before reauthorizing; initial MVP does **not** provide automatic retry recovery or automatic PR merge detection.
- A large feature, commercial payments, ads, internet downloads or platform releases should be decomposed into explicitly approved new phases—not smuggled into the initial plan.

## Validation and tests

```bash
python3 -m unittest discover -s tests -v
python3 orchestrator.py validate-plan plan.example.game.yaml
python3 orchestrator.py --help
```

Run `flutter doctor -v` and platform smoke tests separately; the Python tests do not create tickets, run Codex or exercise a real Flutter project.
