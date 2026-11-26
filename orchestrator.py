#!/usr/bin/env python3
"""Jira -> Codex -> draft GitHub PR MVP (Jira Cloud, Python 3.11+)."""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "plan.schema.yaml"


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    url: str
    email: str
    token: str
    project: str
    repo: Path
    base_branch: str
    ready_status: str
    in_progress_status: str
    review_status: str
    blocked_status: str
    ready_label: str
    check_command: str
    worktree_root: Path
    codex_timeout: int
    test_timeout: int

    @staticmethod
    def from_env() -> "Config":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Set {name} in .env")
            return value

        url = required("JIRA_BASE_URL").rstrip("/")
        if not url.startswith("https://"):
            raise ValueError("JIRA_BASE_URL must be https://")
        project = required("JIRA_PROJECT_KEY")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
            raise ValueError("Unexpected JIRA_PROJECT_KEY")
        base_branch = os.getenv("GIT_BASE_BRANCH", "main")
        if not re.fullmatch(r"[\w./-]+", base_branch) or ".." in base_branch:
            raise ValueError("Unsafe GIT_BASE_BRANCH")
        return Config(
            url=url, email=required("JIRA_EMAIL"), token=required("JIRA_API_TOKEN"),
            project=project, repo=Path(required("REPO_PATH")).expanduser().resolve(),
            base_branch=base_branch,
            ready_status=os.getenv("JIRA_READY_STATUS", "Ready"),
            in_progress_status=os.getenv("JIRA_IN_PROGRESS_STATUS", "In Progress"),
            review_status=os.getenv("JIRA_REVIEW_STATUS", "In Review"),
            blocked_status=os.getenv("JIRA_BLOCKED_STATUS", "Blocked"),
            ready_label=os.getenv("JIRA_READY_LABEL", "codex-ready"),
            check_command=required("CHECK_COMMAND"),
            worktree_root=Path(os.getenv("WORKTREE_ROOT", "~/.codex-jira-worktrees")).expanduser().resolve(),
            codex_timeout=int(os.getenv("CODEX_TIMEOUT_SECONDS", "1800")),
            test_timeout=int(os.getenv("TEST_TIMEOUT_SECONDS", "600")),
        )


def adf(text: str) -> dict:
    paragraphs = []
    for line in text.splitlines() or [text]:
        content = [{"type": "text", "text": line[:25000]}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph", "content": []}]}


def plain(doc: object) -> str:
    if isinstance(doc, str):
        return doc
    if isinstance(doc, list):
        return "\n".join(filter(None, (plain(x) for x in doc)))
    if isinstance(doc, dict):
        if doc.get("type") == "text":
            return str(doc.get("text", ""))
        return plain(doc.get("content", []))
    return ""


class Jira:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        auth = base64.b64encode(f"{cfg.email}:{cfg.token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def call(self, method: str, path: str, data: dict | None = None) -> dict:
        url = self.cfg.url + "/rest/api/3" + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise RuntimeError(f"Jira {method} {path} returned HTTP {exc.code}: {detail}") from exc

    def ready_issues(self) -> list[dict]:
        cfg = self.cfg
        def jql_quote(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        query = (
            f'project = {cfg.project} AND status = {jql_quote(cfg.ready_status)} '
            f'AND labels = {jql_quote(cfg.ready_label)} '
            'ORDER BY priority DESC, created ASC'
        )
        response = self.call("POST", "/search/jql", {
            "jql": query, "fields": ["summary", "status", "labels"], "maxResults": 50
        })
        return response.get("issues", [])

    def issue(self, key: str) -> dict:
        fields = "summary,description,status,labels,issuetype,issuelinks,parent"
        return self.call("GET", f"/issue/{urllib.parse.quote(key)}?fields={fields}")

    def dependencies_satisfied(self, issue: dict) -> bool:
        # A Jira link of type 'Blocks' has 'inwardIssue' on the blocked ticket.
        for link in issue.get("fields", {}).get("issuelinks", []):
            relationship = link.get("type", {}).get("inward", "").casefold()
            blocker = link.get("inwardIssue")
            if blocker and relationship in {"is blocked by", "blocked by"}:
                linked = self.issue(blocker["key"])
                category = linked.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key", "")
                if category.casefold() != "done":
                    return False
        return True

    def transition(self, key: str, target: str) -> None:
        transitions = self.call("GET", f"/issue/{key}/transitions").get("transitions", [])
        matches = [t for t in transitions if t.get("to", {}).get("name", "").casefold() == target.casefold()]
        if not matches:
            allowed = ", ".join(t.get("to", {}).get("name", "?") for t in transitions)
            raise RuntimeError(f"No Jira transition from {key} to '{target}'. Available destinations: {allowed}")
        self.call("POST", f"/issue/{key}/transitions", {"transition": {"id": matches[0]["id"]}})

    def comment(self, key: str, message: str) -> None:
        self.call("POST", f"/issue/{key}/comment", {"body": adf(message[:12000])})

    def create_issue(self, summary: str, description: str, issue_type: str,
                     parent: str | None = None, labels: list[str] | None = None) -> str:
        fields: dict = {
            "project": {"key": self.cfg.project},
            "issuetype": {"name": issue_type},
            "summary": summary[:250],
            "description": adf(description),
            "labels": labels or ["codex-planned"],
        }
        if parent:
            fields["parent"] = {"key": parent}
        return self.call("POST", "/issue", {"fields": fields})["key"]

    def blocks(self, blocker: str, blocked: str) -> None:
        self.call("POST", "/issueLink", {
            "type": {"name": "Blocks"},
            "outwardIssue": {"key": blocker},
            "inwardIssue": {"key": blocked},
        })


def safe_env() -> dict[str, str]:
    # Secrets are needed by the controller, not by the model or project tests.
    return {k: v for k, v in os.environ.items()
            if k not in {"JIRA_API_TOKEN", "JIRA_EMAIL", "GH_TOKEN", "GITHUB_TOKEN"}}


def run_command(args: list[str], cwd: Path | None = None, timeout: int = 120,
                env: dict | None = None) -> str:
    completed = subprocess.run(args, cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"Command failed ({completed.returncode}): {args[0]}\n{completed.stdout[-4000:]}")
    return completed.stdout.strip()


def codex_args(repo: Path, sandbox: str = "workspace-write") -> list[str]:
    return ["codex", "exec", "-C", str(repo), "--sandbox", sandbox,
            "--ask-for-approval", "never",
            "-c", "mcp_servers.atlassian.enabled=false"]



def read_plan(path: Path) -> dict:
    """Read a YAML or legacy JSON plan; never execute YAML object tags."""
    extension = path.suffix.lower()
    if extension not in {".yaml", ".yml", ".json"}:
        raise ValueError("Plan must use .yaml, .yml or .json extension")
    source = path.read_text(encoding="utf-8")
    try:
        plan = json.loads(source) if extension == ".json" else yaml.safe_load(source)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid plan file {path}: {exc}") from exc
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a mapping/object with project and epics")
    return plan


def write_plan(path: Path, plan: dict) -> None:
    """Write human-editable YAML (default), preserving legacy JSON support."""
    extension = path.suffix.lower()
    if extension not in {".yaml", ".yml", ".json"}:
        raise ValueError("Plan output must end with .yaml, .yml or .json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if extension == ".json":
        content = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    else:
        content = yaml.safe_dump(plan, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(content, encoding="utf-8")


def load_plan_schema(path: Path = SCHEMA) -> dict:
    """Read YAML JSON Schema, validate the schema definition itself."""
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot read YAML schema {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("Plan schema must be a YAML mapping")
    Draft202012Validator.check_schema(schema)
    return schema


def validate_plan_schema(plan: dict, schema: dict) -> None:
    """Validate plan shape using plan.schema.yaml; no Jira operations."""
    errors = sorted(Draft202012Validator(schema).iter_errors(plan),
                    key=lambda error: tuple(str(p) for p in error.absolute_path))
    if errors:
        error = errors[0]
        path = ".".join(str(p) for p in error.absolute_path) or "<root>"
        raise ValueError(f"Schema validation failed at {path}: {error.message}")


def validate_plan(plan: dict, project: str) -> None:
    validate_plan_schema(plan, load_plan_schema())
    if plan.get("project") != project:
        raise ValueError(f"Plan project must be {project}")
    epics = plan.get("epics")
    if not isinstance(epics, list) or not epics:
        raise ValueError("Plan must contain at least one epic")
    ids: set[str] = set()
    edges: dict[str, list[str]] = {}
    for epic in epics:
        if not isinstance(epic, dict) or any(
            not isinstance(epic.get(field), str) or not epic[field].strip()
            for field in ("id", "summary", "description")
        ):
            raise ValueError("Each epic needs nonempty id, summary, and description")
        if epic["id"] in ids:
            raise ValueError(f"Duplicate id: {epic['id']}")
        ids.add(epic["id"])
        if not isinstance(epic.get("tasks"), list):
            raise ValueError(f"Epic {epic['id']} needs tasks list")
        for task in epic["tasks"]:
            if not isinstance(task, dict) or any(
                not isinstance(task.get(field), str) or not task[field].strip()
                for field in ("id", "summary", "description")
            ):
                raise ValueError("Each task needs nonempty id, summary, and description")
            task_id = task["id"]
            criteria = task.get("acceptance_criteria")
            deps = task.get("depends_on")
            if not isinstance(criteria, list) or not criteria or not all(
                isinstance(value, str) and value.strip() for value in criteria
            ):
                raise ValueError(f"Task {task_id} needs nonempty acceptance_criteria strings")
            if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
                raise ValueError(f"Task {task_id} needs depends_on list of task IDs")
            if task_id in ids:
                raise ValueError(f"Duplicate id: {task_id}")
            ids.add(task_id)
            edges[task_id] = deps
    for task, dependencies in edges.items():
        if any(dep not in edges or dep == task for dep in dependencies):
            raise ValueError(f"Invalid dependency for {task}: {dependencies}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visiting:
            raise ValueError(f"Dependency cycle involving {node}")
        if node in visited:
            return
        visiting.add(node)
        for dep in edges[node]:
            walk(dep)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        walk(node)


def plan_command(cfg: Config, brief_path: Path, output_path: Path) -> None:
    brief = brief_path.read_text(encoding="utf-8")
    if not brief.strip():
        raise ValueError("Brief cannot be empty")
    output_path = output_path.resolve()
    if output_path.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ValueError("Plan output must end with .yaml, .yml or .json")
    prompt = f"""You are the tech lead planning a development project.
Read the repository and the brief below. Return ONLY JSON matching the provided schema.
Project key: {cfg.project}
Make 2-5 epics and reasonably scoped tasks. Each task has specific, verifiable acceptance criteria.
Use stable ids like 'auth' or 'auth-login-api'; depends_on refers to task ids.
Represent tasks in likely implementation order and keep dependencies acyclic.
Do not modify files, call Jira, or implement any code.

BRIEF:\n{brief[:30000]}"""
    print("Generating proposed plan (does NOT create Jira tickets)...")
    # Codex expects a JSON Schema in JSON format for --output-schema.
    # The canonical source is YAML; convert it to a temporary JSON file.
    with tempfile.TemporaryDirectory(prefix="codex-jira-plan-") as tmp_dir:
        model_output = Path(tmp_dir) / "codex-plan.json"
        json_schema = Path(tmp_dir) / "plan.schema.json"
        json_schema.write_text(json.dumps(load_plan_schema(), ensure_ascii=False), encoding="utf-8")
        run_command(codex_args(cfg.repo, "read-only") + ["--output-schema", str(json_schema),
                    "--output-last-message", str(model_output), prompt], cwd=cfg.repo,
                    timeout=cfg.codex_timeout, env=safe_env())
        content = json.loads(model_output.read_text(encoding="utf-8"))
    validate_plan(content, cfg.project)
    write_plan(output_path, content)
    print(f"Plan generated: {output_path}\nReview and edit this file before apply-plan.")


def apply_plan(cfg: Config, plan_path: Path) -> None:
    plan = read_plan(plan_path)
    validate_plan(plan, cfg.project)
    state_file = plan_path.with_name(plan_path.stem + ".applied.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {"issues": {}, "links": []}
    jira = Jira(cfg)

    def persist():
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(state_file)

    for epic in plan["epics"]:
        if epic["id"] not in state["issues"]:
            issue_key = jira.create_issue(epic["summary"], epic["description"], "Epic")
            state["issues"][epic["id"]] = issue_key
            persist()
            print(f"Epic {epic['id']} -> {issue_key}")
        epic_key = state["issues"][epic["id"]]
        for task in epic["tasks"]:
            if task["id"] not in state["issues"]:
                criteria = "\n".join(f"- {x}" for x in task["acceptance_criteria"])
                description = f"{task['description']}\n\nAcceptance criteria:\n{criteria}"
                issue_key = jira.create_issue(task["summary"], description, "Task", parent=epic_key)
                state["issues"][task["id"]] = issue_key
                persist()
                print(f"Task {task['id']} -> {issue_key}")
    for epic in plan["epics"]:
        for task in epic["tasks"]:
            for dep in task["depends_on"]:
                pair = f"{dep}>{task['id']}"
                if pair not in state["links"]:
                    jira.blocks(state["issues"][dep], state["issues"][task["id"]])
                    state["links"].append(pair)
                    persist()
                    print(f"Dependency {pair}")
    print(f"Created/reconciled tickets; mapping: {state_file}")
    print("None are auto-authorized. In Jira, transition a task to Ready and add label codex-ready.")


def ensure_git_repo(cfg: Config) -> None:
    actual = Path(run_command(["git", "-C", str(cfg.repo), "rev-parse", "--show-toplevel"])).resolve()
    if actual != cfg.repo:
        raise ValueError(f"REPO_PATH must be Git repository root: {actual}")


def execute_ticket(cfg: Config, jira: Jira, issue: dict) -> None:
    key = issue["key"]
    fields = issue["fields"]
    title = fields["summary"]
    description = plain(fields.get("description"))
    root = cfg.worktree_root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    worktree = root / key
    branch = f"codex/{key}"
    issue_url = f"{cfg.url}/browse/{key}"
    log = logs / f"{key}.log"
    last_message = logs / f"{key}.last.md"
    jira.transition(key, cfg.in_progress_status)
    try:
        run_command(["git", "-C", str(cfg.repo), "fetch", "origin", cfg.base_branch], timeout=180)
        if worktree.exists():
            raise RuntimeError(f"Existing worktree at {worktree}; investigate before retry")
        run_command(["git", "-C", str(cfg.repo), "worktree", "add", "-b", branch,
                     str(worktree), f"origin/{cfg.base_branch}"])
        prompt = f"""Implement Jira {key} in this repository.
Title: {title}
Description and acceptance criteria:\n{description[:30000]}
Jira: {issue_url}

Follow AGENTS.md and repository conventions. Implement the smallest coherent change.
Add or update meaningful tests. Run relevant tests if tools permit.
Do NOT create a git commit, change branches, push, open PRs, contact Jira,
or modify files outside this workspace. Never access or print secrets.
If the task is ambiguous or unsafe, do not improvise; state blockers.
End with a concise summary of changes, tests, and any outstanding risks.
"""
        cmd = codex_args(worktree) + ["--output-last-message", str(last_message), prompt]
        completed = subprocess.run(cmd, cwd=worktree, env=safe_env(),
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=cfg.codex_timeout)
        log.write_text(completed.stdout + "\n--- STDERR ---\n" + completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(f"Codex failed with exit {completed.returncode}; inspect {log}")
        print(f"Running configured verification: {cfg.check_command}")
        # CHECK_COMMAND is supplied by the operator in .env, never by Jira or Codex.
        verification = subprocess.run(["bash", "-lc", cfg.check_command], cwd=worktree,
                                      env=safe_env(), capture_output=True, text=True,
                                      timeout=cfg.test_timeout)
        with log.open("a", encoding="utf-8") as out:
            out.write("\n--- VERIFICATION ---\n" + verification.stdout + "\n" + verification.stderr)
        if verification.returncode:
            raise RuntimeError(f"CHECK_COMMAND failed; inspect {log}")
        run_command(["git", "-C", str(worktree), "add", "-A"])
        staged = run_command(["git", "-C", str(worktree), "diff", "--cached", "--name-only"])
        if not staged:
            raise RuntimeError("Codex completed but produced no file changes")
        forbidden = [name for name in staged.splitlines() if
                     re.search(r"(^|/)(\.env($|\.)|id_rsa$|id_ed25519$)|\.(pem|p12|pfx|key)$", name, re.I)]
        if forbidden:
            raise RuntimeError(f"Refusing to commit possible secrets: {forbidden}")
        run_command(["git", "-C", str(worktree), "commit", "-m", f"{key}: {title[:70]}"])
        run_command(["git", "-C", str(worktree), "push", "-u", "origin", branch], timeout=180)
        summary = last_message.read_text(encoding="utf-8")[:3500] if last_message.exists() else ""
        pr_body = f"Jira: {issue_url}\n\nAutomated implementation; human review required.\n\n{summary}"
        pr_url = run_command(["gh", "pr", "create", "--draft", "--base", cfg.base_branch,
                              "--head", branch, "--title", f"{key}: {title[:110]}",
                              "--body", pr_body], cwd=worktree, timeout=120).splitlines()[-1]
        jira.comment(key, f"Codex implementation is ready for review.\nDraft PR: {pr_url}\nConfigured verification passed: {cfg.check_command}")
        jira.transition(key, cfg.review_status)
        print(f"Created draft PR: {pr_url}\nJira: {issue_url} now {cfg.review_status}")
        run_command(["git", "-C", str(cfg.repo), "worktree", "remove", "--force", str(worktree)])
    except Exception as exc:
        print(f"Failed on {key}: {exc}", file=sys.stderr)
        try:
            jira.comment(key, f"Codex worker stopped: {str(exc)[:1500]}\nManual intervention needed.\nWorktree (if created): {worktree}")
            try:
                jira.transition(key, cfg.blocked_status)
            except Exception as status_error:
                print(f"Could not transition to {cfg.blocked_status}: {status_error}", file=sys.stderr)
        except Exception as comment_error:
            print(f"Could not write failure to Jira: {comment_error}", file=sys.stderr)
        raise


@contextlib.contextmanager
def single_worker_lock(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A worker is already running on this machine") from exc
        yield


def once(cfg: Config, dry_run: bool = False) -> None:
    ensure_git_repo(cfg)
    jira = Jira(cfg)
    with single_worker_lock(cfg.worktree_root):
        candidates = jira.ready_issues()
        if not candidates:
            print("No Ready + codex-ready tasks")
            return
        for candidate in candidates:
            issue = jira.issue(candidate["key"])
            if issue["fields"].get("issuetype", {}).get("name", "").casefold() not in {"task", "story", "bug", "sub-task", "subtask"}:
                continue
            if not jira.dependencies_satisfied(issue):
                print(f"Skipping {issue['key']}: blocked by dependency")
                continue
            if dry_run:
                print(f"DRY RUN: would implement {issue['key']}: {issue['fields']['summary']}")
                return
            execute_ticket(cfg, jira, issue)
            return
        print("No unblocked executable tasks found among first 50 matching tickets")


def main() -> None:
    os.umask(0o077)
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan", help="Generate plan YAML for human review (no Jira writes)")
    plan_parser.add_argument("brief", type=Path)
    plan_parser.add_argument("--out", type=Path, default=ROOT / "plan.yaml")
    apply_parser = sub.add_parser("apply-plan", help="Create Jira Epics, Tasks, and links")
    apply_parser.add_argument("plan", type=Path)
    apply_parser.add_argument("--yes", action="store_true", help="Confirm creating Jira tickets")
    validate_parser = sub.add_parser("validate-plan", help="Validate YAML/JSON without Jira changes")
    validate_parser.add_argument("plan", type=Path)
    once_parser = sub.add_parser("once", help="Run one Ready Jira task")
    once_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "validate-plan":
        project = os.getenv("JIRA_PROJECT_KEY", "").strip()
        if not project:
            parser.error("Set JIRA_PROJECT_KEY in .env to validate a plan")
        validate_plan(read_plan(args.plan), project)
        print(f"Valid plan for {project}: {args.plan}")
        return
    cfg = Config.from_env()
    if args.command == "plan":
        ensure_git_repo(cfg)
        plan_command(cfg, args.brief, args.out)
    elif args.command == "apply-plan":
        if not args.yes:
            parser.error("apply-plan writes Jira tickets; review the YAML first and add --yes")
        apply_plan(cfg, args.plan)
    elif args.command == "once":
        once(cfg, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
