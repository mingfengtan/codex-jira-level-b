# Codex + Jira Level B: semi-autonomous software delivery (MVP)

This is a **local, single-worker starter**. It generates a proposed Jira hierarchy from a product brief, requires approval before creating issues, then processes one Jira ticket per run into a **draft** GitHub PR. The human reviewer decides whether to merge and mark Done.

Works with **Jira Cloud**, GitHub, a local Git repository, Python 3.11+, and Codex CLI. Developed for macOS/Linux/WSL. Install the Python dependencies **PyYAML** and **jsonschema** using `requirements.txt`.

> **Not a production-grade distributed orchestrator.** Single-machine file locking is used. The workflow expects manual recovery after an interrupted run and should execute only in a controlled, trusted repository.

## 0. Architecture

```
You / Codex (VS Code) --product brief--> plan.yaml
      |                                  |
      |                   human review + apply-plan
      |                                  |
      v                                  v
Atlassian MCP (interactive)          Jira Epic -> Task + dependencies
                                         |
                               Ready + codex-ready label
                                         |
                                 cron / manual CLI
                                         |
                               Python orchestrator
                                 |             |
                         Jira REST API     Codex CLI exec
                                              |
                                         Git worktree
                                              |
                                         CHECK_COMMAND
                                              |
                                           draft PR
                                              |
                                 Jira: In Review
                                              |
                                  HUMAN reviews/merges
                                              |
                                       Jira: Done
```

## 1. Prerequisites

Install and authenticate the official Codex CLI and GitHub CLI. Run in your VS Code terminal:

```bash
npm install -g @openai/codex
codex login
codex --version

# macOS: brew install gh    (Linux/Windows: install GitHub CLI from its docs)
gh auth login
gh auth status

# Connect Atlassian MCP for **interactive** Jira work in Codex/VS Code
codex mcp add atlassian --url https://mcp.atlassian.com/v2/mcp
codex mcp login atlassian
codex mcp list
```

Codex CLI + the VS Code Codex extension share Codex MCP configuration. Atlassian's recommended endpoint is `/v2/mcp`.

> The **background worker** uses Jira REST directly instead of MCP, which makes status transitions deterministic and lets the execution agent run without Jira mutation privileges.

## 2. Jira and GitHub configuration

1. Create a Jira Cloud software project, e.g. **APP**, manually in Jira. New Jira project creation requires appropriate admin permissions, and is not the same as creating an Epic.
2. Set up statuses **Ready**, **In Progress**, **In Review**, **Blocked**, **Done** and workflow transitions between them. If you already use **To Do**, set `JIRA_READY_STATUS=To Do` instead. Ensure transitions to In Progress, In Review, and Blocked are accessible to the bot account.
3. Create a **standard/non-scoped Atlassian API token** for Jira Cloud REST access at https://id.atlassian.com/manage-profile/security/api-tokens. A scoped token may require Atlassian's API gateway and a different base URL, which this starter does not implement. Prefer a dedicated automation account with limited permissions.
4. Verify your target repository has an `origin` remote with a GitHub repository, and that `origin/main` (or your base branch) exists. Configure Git commit name/email on the machine.
5. Configure branch protection and CI in GitHub. Require human review before merge; prohibit direct push to the protected main branch.
6. Copy `AGENTS.example.md` to `AGENTS.md` in your **target Git repository**, adjusting the project's test/build conventions.

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
# Edit .env values, especially: JIRA_*, REPO_PATH, CHECK_COMMAND
python3 --version   # 3.11+
```

`REPO_PATH` must be the absolute **root** of your existing Git repository, not the path to this orchestrator.

`CHECK_COMMAND` is a **required command controlled by you**, such as `npm test -- --run`, `pnpm test -- --run`, or `python -m pytest -q`. It runs independently of Codex; choose a command that exits successfully only if required checks pass. Ensure test dependencies are installed or cached before running unattended.

Do not commit `.env`. The starter strips `JIRA_API_TOKEN`, `JIRA_EMAIL`, `GH_TOKEN`, and `GITHUB_TOKEN` from the Codex and test subprocess environments. If the app uses other secrets, run the worker in an isolated VM/container with only the minimum credentials.

## 3. Test the starter

```bash
python3 -m unittest discover -s tests -v
python3 orchestrator.py once --dry-run
```

Dry-run queries Jira for up to 50 matching candidates, checks dependency links, and reports one eligible task. It does **not** run Codex, modify Jira, or push code.

## 4. Generate your project plan (optionally using Codex)

Edit `project_brief.example.md` (or write your own). Generate a proposed project plan from the repository context:

```bash
python3 orchestrator.py plan project_brief.example.md --out plan.yaml
# Or omit --out; plan.yaml is the default output.
python3 orchestrator.py validate-plan plan.yaml
```

Read and edit `plan.yaml` **before** applying. The plan includes stable IDs, Epics, Tasks, acceptance criteria, and dependencies. `plan.schema.yaml` is the only schema source of truth. The orchestrator converts it to a temporary JSON Schema for Codex structured output, validates the result with Python `jsonschema`, then emits YAML via `yaml.safe_dump`. `apply-plan` uses `yaml.safe_load`. Legacy `.json` plans still work. Start with `plan.example.yaml` if you would rather write a plan by hand.

After approval:

```bash
python3 orchestrator.py validate-plan plan.yaml
python3 orchestrator.py apply-plan plan.yaml --yes
```

The script creates Jira Epics and their Tasks using the `parent` relationship, then links blocked tasks with Jira's **Blocks** issue link type. It saves `plan.applied.json` to remember created IDs and links, allowing recovery/re-entry if an intermediate API request fails. If Jira's issue type names or parent hierarchy differ, update `create_issue()` for your project before applying.

**No newly created tickets are authorized to execute**. In Jira, review a candidate task, move it to Ready, and add the `codex-ready` label.

Alternatively, with Atlassian MCP connected, ask VS Code Codex to **propose** the Jira hierarchy and create approved tickets interactively. Use one approach for initial planning to avoid duplicate issues.

### YAML plan shape

The supported YAML plan uses exactly the same logical structure as the original JSON plan:

```yaml
project: AIDEV
# Set to the value of JIRA_PROJECT_KEY in .env
epics:
  - id: auth
    summary: Authentication
    description: User login and sessions
    tasks:
      - id: auth-api
        summary: Implement login API
        description: API endpoint and tests
        acceptance_criteria:
          - Invalid credentials return 401.
        depends_on: []
```

Task `depends_on` entries reference the **stable task IDs inside the plan** (for example `auth-api`), not Jira issue keys. Task dependencies can cross Epics. The mapping to real Jira issue keys stays in `plan.applied.json`; don't delete that file after applying a plan. Do **not** reapply a different plan with unrelated tasks under the same filename, as the mapping file is used for idempotency.

The output extension determines the format: `.yaml` or `.yml` for YAML, `.json` for backward-compatible JSON. Use `validate-plan` to check a hand-edited YAML file against `plan.schema.yaml` and enforce project/dependency rules without making Jira changes. YAML is loaded with `yaml.safe_load`, which does not instantiate arbitrary Python objects. YAML supports comments and block scalars (`>` and `|`) if you want to document the plan. You may also modify `plan.schema.yaml` for additional compatible validation constraints; note that Codex structured output accepts only a subset of JSON Schema features, so keep generation-compatible constraints in the schema and business rules in the Python validator.

## 5. Execute one ticket

```bash
python3 orchestrator.py once --dry-run
python3 orchestrator.py once
```

What happens:

1. Search Jira with **POST `/rest/api/3/search/jql`** for issues in the project with `status = Ready` and `labels = codex-ready`, sorted by priority and creation time.
2. Ignore Epics, check **Blocks** dependencies, choose one eligible task.
3. Transition Jira **Ready → In Progress**.
4. Fetch `origin/main`, create a dedicated Git worktree at `~/.codex-jira-worktrees/APP-123` on branch `codex/APP-123`.
5. Run `codex exec -C <worktree> --sandbox workspace-write --ask-for-approval never` with the ticket's description and acceptance criteria. Atlassian MCP is disabled for this agent invocation.
6. Run your `CHECK_COMMAND` separately; fail closed on nonzero exit.
7. Stage changes, reject common secret-file names, commit and push the feature branch, create a **draft PR** with `gh pr create --draft`.
8. Comment in Jira with the PR URL and test result; transition to **In Review**.
9. After **you** approve, merge the PR and mark Jira **Done** (manually in MVP).

On an exception, the worker comments on Jira, *attempts* to transition to Blocked, and retains the worktree for inspection. Some Jira workflows do not support a Blocked transition; in that case it leaves the task In Progress. This is intentional: no silent retries of partially completed work.

The script uses a single-machine lock and starts **at most one task per invocation**. Multiple machines are **not** coordinated; do not run two different machines against the same label queue.

## 6. Schedule it (macOS/Linux/WSL)

After confirming manual execution works, add a cron entry (`crontab -e`) to run every 30 minutes:

```cron
*/30 * * * * /ABSOLUTE/PATH/TO/python3 /ABSOLUTE/PATH/TO/codex-jira-level-b/orchestrator.py once >> /ABSOLUTE/PATH/TO/worker.log 2>&1
```

The cron environment must have the `codex`, `git`, and `gh` executables on `PATH`; `codex login` and `gh auth login` must have been completed **for the same OS user**. Test from a non-interactive shell. Under Node version managers (nvm, fnm, etc.), use a wrapper script that sets the explicit binary paths.

Example daily execution budget: once every 30 minutes, but **one task per run**. To enforce a hard daily cap, add run accounting before expanding to higher autonomy. A worker may consume Codex plan quota and remains subject to model availability and rate limits.

## 7. Working in VS Code

Open your existing Git repository in VS Code. For manual project planning or tickets, use the Codex extension (connected to Atlassian MCP). For automated tickets, the Python worker runs a separate CLI Codex agent in a Git worktree; it does not require the VS Code window to stay open, but the machine and scheduler must remain online.

Helpful VS Code prompt:

```
Read Jira APP-42 via Atlassian MCP. Review its acceptance criteria and the
current code. Suggest missing subtasks and dependencies, but don't create
anything until I approve. Do not implement code until it's authorized.
```

## 8. Deliberate MVP limitations

- Does **not** autonomously add surprise subtasks during implementation. Agents can suggest them in their summary; add them via planning review. This avoids uncontrolled task proliferation.
- Does **not** automatically merge PRs, mark Done, or deploy to production.
- Uses Jira status + label as an **authorization signal**, not an atomic multi-host task lock. Single-machine `flock` prevents local overlaps only.
- `Blocks` dependency detection recognizes standard English Jira **is blocked by** inward issue links. Customize for different link types or locales.
- A crash may strand a ticket In Progress; inspect local worktrees, PRs, and Jira comments before resuming.
- No automatic retries or task timeboxing beyond command timeouts.
- No CI check verification after push. Human reviewer must inspect GitHub CI status before merging.
- Jira updates can lag in enhanced JQL search. Do not run parallel workers without atomic claims.
- `--sandbox workspace-write` is a practical boundary, **not** a substitute for process isolation. Model-generated code and repository test scripts can be hostile; run with least privilege in a disposable VM/container for unfamiliar repositories.
- Does not handle nonstandard Jira configurations, complex workflows, service-managed project screens, custom field requirements, or restricted issue permissions. Validate on a test project first.

## 9. Next incremental improvements

1. Add **scheduled reconciliation**: on GitHub `pull_request.closed` merged events, verify CI/merge status then transition Jira to Done.
2. Add a `followups.json` review queue so the planner creates suggested child tickets only after approval.
3. Add GitHub Actions/hosted runner isolation and a secrets manager.
4. Add a durable task state DB with atomic leases for safe parallel workers.
5. Add digest notifications with blocked tickets, draft PRs, and usage budget.

## Reference docs

- Atlassian MCP: https://atlassian.github.io/atlassian-mcp-server/
- Codex non-interactive: https://learn.chatgpt.com/docs/non-interactive-mode
- Codex CLI reference: https://learn.chatgpt.com/docs/cli/reference
- Jira enhanced JQL search: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
- Jira issues and transitions: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
- Jira basic auth: https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
- GitHub PRs: https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request


### YAML schema as the source of truth

`plan.schema.yaml` contains a **JSON Schema represented as YAML**, not a different schema language. The orchestrator uses it in two ways:

1. `validate-plan` / `apply-plan`: parse both the plan and schema using `yaml.safe_load`, validate the plan with `jsonschema`, then check project-key and task-dependency rules.
2. `plan`: convert the same YAML schema to temporary JSON for `codex exec --output-schema`, then validate Codex's response and write `plan.yaml`. The temporary JSON is removed automatically.

There is **no maintained `plan.schema.json`**.

