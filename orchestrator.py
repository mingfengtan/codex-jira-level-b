#!/usr/bin/env python3
"""Jira -> Codex -> draft GitHub PR MVP (Jira Cloud, Python 3.11+)."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import time
from datetime import datetime, timedelta, timezone
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
    quota_retry_seconds: int = 1800
    retry_base_seconds: int = 120
    max_retries: int = 3
    max_test_repairs: int = 2

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
            quota_retry_seconds=max(60, int(os.getenv("QUOTA_RETRY_SECONDS", "1800"))),
            retry_base_seconds=max(10, int(os.getenv("RETRY_BASE_SECONDS", "120"))),
            max_retries=max(1, int(os.getenv("MAX_RETRIES", "3"))),
            max_test_repairs=max(0, int(os.getenv("MAX_TEST_REPAIRS", "2"))),
        )


def adf(text: str, *, linkify: bool = False) -> dict:
    """Produce Jira Cloud ADF. For comments, create real clickable URL marks.

    Jira REST API v3 does not automatically convert plain URL text into link
    marks. Keep issue descriptions unchanged; linkify controller comments.
    """
    paragraphs = []
    url_re = re.compile(r"https://[^\s<>\[\]\(\)]+")
    for line in text.splitlines() or [text]:
        line = line[:25000]
        if not line:
            paragraphs.append({"type": "paragraph", "content": []})
            continue
        if not linkify:
            nodes = [{"type": "text", "text": line}]
        else:
            nodes = []
            cursor = 0
            for found in url_re.finditer(line):
                url = found.group(0).rstrip(".,;:!?")
                if not url:
                    continue
                if found.start() > cursor:
                    nodes.append({"type": "text", "text": line[cursor:found.start()]})
                nodes.append({"type": "text", "text": url,
                              "marks": [{"type": "link", "attrs": {"href": url}}]})
                cursor = found.start() + len(url)
            if cursor < len(line):
                nodes.append({"type": "text", "text": line[cursor:]})
        paragraphs.append({"type": "paragraph", "content": nodes})
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
        fields = "summary,description,status,labels,issuetype,issuelinks,parent,subtasks"
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
        self.call("POST", f"/issue/{key}/comment", {"body": adf(message[:12000], linkify=True)})

    def has_comment_marker(self, key: str, marker: str) -> bool:
        """Check existing comments so a crash during Jira sync cannot post twice."""
        start = 0
        while True:
            reply = self.call("GET", f"/issue/{urllib.parse.quote(key)}/comment?startAt={start}&maxResults=100")
            comments = reply.get("comments", [])
            if any(marker in plain(comment.get("body")) for comment in comments):
                return True
            start += len(comments)
            if not comments or start >= reply.get("total", start):
                return False

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
        if issue_type == "Subtask":
            # Jira sites use either 'Sub-task' or 'Subtask'; use its actual ID.
            metadata = self.call("GET", f"/project/{urllib.parse.quote(self.cfg.project)}")
            candidates = [t for t in metadata.get("issueTypes", []) if t.get("subtask") is True]
            if not candidates:
                raise RuntimeError("Subtask work type is not enabled in the Jira space")
            fields["issuetype"] = {"id": candidates[0]["id"]}
        return self.call("POST", "/issue", {"fields": fields})["key"]

    def blocks(self, blocker: str, blocked: str) -> None:
        """Create blocker -> blocked, using Jira Cloud's link-end conventions.

        For Jira's standard Blocks type, the *blocked* issue is the
        outwardIssue of the creation request, and the *blocker* is the
        inwardIssue. On GET blocked issue, blocker then appears in
        issuelinks[].inwardIssue as 'is blocked by'.
        """
        self.call("POST", "/issueLink", {
            "type": {"name": "Blocks"},
            "outwardIssue": {"key": blocked},
            "inwardIssue": {"key": blocker},
        })

    def remove_link(self, link_id: str) -> None:
        # Link IDs originate from GET /issue and are never fabricated.
        self.call("DELETE", f"/issueLink/{urllib.parse.quote(str(link_id), safe='')}")


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


def run_plan_codex(args: list[str], cwd: Path, timeout: int, log_path: Path,
                   heartbeat_seconds: float = 15.0) -> None:
    """Stream Codex JSONL events into a durable log and show concise progress.

    This is one asynchronous subprocess, not concurrent planning. The JSONL log
    intentionally contains raw Codex events; it may contain project details.
    Do not commit or share it unreviewed.
    """
    log_path = log_path.expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Codex event log: {log_path}", flush=True)
    print(f"Follow from another terminal: tail -f {log_path}", flush=True)
    started = time.monotonic()
    last_heartbeat = started
    last_activity = started
    stdout_tail = bytearray()
    line_buffer = bytearray()
    event_failures: list[str] = []
    session_id = None
    process = subprocess.Popen(
        args[:-1] + ["--json", args[-1]] if "--json" not in args else args,
        cwd=cwd, env=safe_env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    def write_event(log, event: dict) -> None:
        log.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        log.flush()
    def handle_line(raw: bytes, log) -> None:
        nonlocal session_id
        if not raw.strip():
            return
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an event object")
        except (ValueError, UnicodeDecodeError):
            event = {"type": "orchestrator.non_json_output",
                     "text": raw.decode("utf-8", errors="replace")}
        write_event(log, event)
        kind = event.get("type", "unknown")
        elapsed = int(time.monotonic() - started)
        if kind == "thread.started":
            session_id = event.get("thread_id")
            print(f"[{elapsed}s] Codex session started: {session_id}", flush=True)
        elif kind == "turn.started":
            print(f"[{elapsed}s] Codex turn started", flush=True)
        elif kind in {"item.started", "item.completed", "item.updated"}:
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") in {"command_execution", "web_search", "mcp_tool_call"}:
                if kind != "item.updated":
                    print(f"[{elapsed}s] {kind}: {item['type']}", flush=True)
        elif kind == "turn.completed":
            print(f"[{elapsed}s] Codex turn completed", flush=True)
        elif kind in {"error", "turn.failed"}:
            detail = event.get("error")
            # Don't print full model text / secret-containing prompt to terminal.
            excerpt = str(detail)[:350] if detail is not None else kind
            event_failures.append(excerpt)
            print(f"[{elapsed}s] Codex reported {kind}: {excerpt}", flush=True)
        elif kind == "orchestrator.non_json_output":
            print(f"[{elapsed}s] Codex output: {event['text'][:240]}", flush=True)

    try:
        with log_path.open("ab") as log, selectors.DefaultSelector() as selector:
            write_event(log, {"type": "orchestrator.start", "at": instant(),
                              "pid": process.pid, "timeout_seconds": timeout})
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                now = time.monotonic()
                if now - started >= timeout:
                    write_event(log, {"type": "orchestrator.timeout", "after_seconds": timeout,
                                      "session_id": session_id})
                    raise TimeoutError(f"Codex planning timed out after {timeout}s. "
                                       f"Events saved in {log_path}; session={session_id}")
                if now - last_heartbeat >= heartbeat_seconds:
                    print(f"[{int(now-started)}s] Still waiting for Codex; "
                          f"last event {int(now-last_activity)}s ago. Log: {log_path}", flush=True)
                    last_heartbeat = now
                events = selector.select(timeout=0.3)
                for _key, _mask in events:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                        continue
                    last_activity = time.monotonic()
                    stdout_tail.extend(chunk)
                    if len(stdout_tail) > 20000:
                        del stdout_tail[:-20000]
                    line_buffer.extend(chunk)
                    while b"\n" in line_buffer:
                        raw, _, remainder = line_buffer.partition(b"\n")
                        line_buffer = bytearray(remainder)
                        handle_line(raw, log)
                if process.poll() is not None and not selector.get_map():
                    if line_buffer:
                        handle_line(bytes(line_buffer), log)
                        line_buffer.clear()
                    break
            code = process.wait()
            write_event(log, {"type": "orchestrator.exit", "returncode": code,
                              "duration_seconds": int(time.monotonic()-started),
                              "session_id": session_id})
        if code != 0 or event_failures:
            summary = event_failures[-1] if event_failures else stdout_tail.decode(
                "utf-8", errors="replace")[-750:]
            raise RuntimeError(f"Codex plan generation failed (exit={code}). "
                               f"See {log_path}. Last error: {summary}")
    except BaseException:
        terminate_group(process)
        raise
    finally:
        if process.stdout:
            process.stdout.close()


def codex_args(repo: Path, sandbox: str = "workspace-write") -> list[str]:
    # The Codex exec subcommand does not accept --ask-for-approval after exec.
    # Keep the CLI executable explicit so a Python venv cannot shadow it with
    # another application named `codex`.
    codex_bin = os.getenv("OPENAI_CODEX_BIN", "codex").strip()
    if not codex_bin:
        raise ValueError("OPENAI_CODEX_BIN cannot be empty")
    return [codex_bin, "exec", "-C", str(repo), "--sandbox", sandbox,
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


def codex_output_schema(schema: dict) -> dict:
    """Return a strict Codex Structured Outputs schema from the YAML schema.

    The YAML schema remains the authoritative *local validation* schema and may
    have optional fields. Structured Outputs requires every property of every
    object (including nested objects) in `required`. For generated plans those
    fields must all be present; empty `subtasks: []` is valid.

    Do not modify `schema` in-place: local validation intentionally continues
    to accept optional `phase`, `type`, `priority`, and `subtasks`.
    """
    output = copy.deepcopy(schema)

    def walk(node: object) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        # Only descend through known JSON Schema nodes; never through enum,
        # default, description or example values.
        if node.get("type") == "object":
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("Codex output schema object properties must be a mapping")
            node["additionalProperties"] = False
            node["required"] = list(properties)
        for name in ("properties", "$defs", "definitions", "patternProperties"):
            entries = node.get(name)
            if isinstance(entries, dict):
                for child in entries.values():
                    walk(child)
        for name in ("items", "additionalProperties", "not", "if", "then", "else"):
            walk(node.get(name))
        for name in ("allOf", "anyOf", "oneOf", "prefixItems"):
            walk(node.get(name))

    walk(output)
    Draft202012Validator.check_schema(output)
    return output


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
    parents: dict[str, str] = {}
    def check_id(identifier: str) -> None:
        if identifier in ids:
            raise ValueError(f"Duplicate id: {identifier}")
        ids.add(identifier)
    for epic in epics:
        check_id(epic["id"])
        for task in epic["tasks"]:
            check_id(task["id"])
            edges[task["id"]] = task["depends_on"]
            for child in task.get("subtasks", []):
                check_id(child["id"])
                edges[child["id"]] = child["depends_on"]
                parents[child["id"]] = task["id"]
    for task, dependencies in edges.items():
        if any(dep not in edges or dep == task for dep in dependencies):
            raise ValueError(f"Invalid dependency for {task}: {dependencies}")
        if parents.get(task) in dependencies:
            raise ValueError(f"Subtask {task} cannot depend on parent")
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

def plan_command(cfg: Config, brief_path: Path, output_path: Path,
                 log_path: Path | None = None) -> None:
    brief = brief_path.read_text(encoding="utf-8")
    if not brief.strip():
        raise ValueError("Brief cannot be empty")
    output_path = output_path.resolve()
    if output_path.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ValueError("Plan output must end with .yaml, .yml or .json")
    prompt = f"""You are the tech lead planning a development project.
Read the repository and the brief below. Return ONLY JSON matching the provided schema.
Project key: {cfg.project}
Plan ONLY P0 Foundation and P1 Playable MVP, not later roadmap phases.
Create 3-5 epics and 12-20 total small tasks with measurable acceptance criteria.
Every Epic MUST set phase to P0 or P1. No P2+ epics in the initial plan.
The first task must scaffold Flutter in the initially minimal repository.
Use Story for player-facing functionality; Task for engineering infrastructure.
Do not use subtasks unless they create a meaningful non-overlapping split. A parent
with subtasks is for grouping only; the parent must not duplicate child work.
Use stable IDs and depends_on references to plan task/subtask IDs, with no cycles.
Explicitly defer ads, purchases, subscription, online multiplayer, release and
remote download. Order tasks by likely implementation sequence.
Do not modify files, call Jira, or implement any code.

BRIEF:\n{brief[:30000]}"""
    print("Generating proposed plan (does NOT create Jira tickets)...")
    # Codex expects a JSON Schema in JSON format for --output-schema.
    # The canonical source is YAML; convert it to a temporary JSON file.
    with tempfile.TemporaryDirectory(prefix="codex-jira-plan-") as tmp_dir:
        model_output = Path(tmp_dir) / "codex-plan.json"
        json_schema = Path(tmp_dir) / "plan.schema.json"
        # Pin the project identifier in the temporary Codex schema. The model
        # should not invent the Jira key, which is owned by the controller.
        strict_schema = codex_output_schema(load_plan_schema())
        strict_schema["properties"]["project"]["enum"] = [cfg.project]
        json_schema.write_text(json.dumps(strict_schema, ensure_ascii=False), encoding="utf-8")
        run_plan_codex(codex_args(cfg.repo, "read-only") + ["--output-schema", str(json_schema),
                       "--output-last-message", str(model_output), prompt], cwd=cfg.repo,
                       timeout=cfg.codex_timeout,
                       log_path=log_path or ROOT / "logs" / "plan-generation.jsonl",
                       heartbeat_seconds=max(1.0, float(os.getenv("PLAN_HEARTBEAT_SECONDS", "15"))))
        # Preserve the exact model result outside the temporary directory, so
        # schema/semantic validation errors do not throw away a costly run.
        actual_log = (log_path or ROOT / "logs" / "plan-generation.jsonl").expanduser().resolve()
        last_output = actual_log.with_suffix(".last.json")
        last_output.parent.mkdir(parents=True, exist_ok=True)
        last_output.write_bytes(model_output.read_bytes())
        last_output.chmod(0o600)
        print(f"Raw generated plan saved: {last_output}", flush=True)
        content = json.loads(last_output.read_text(encoding="utf-8"))
    # The Jira project is configuration, not a field the model can choose. Even
    # if a provider ignores the single-value enum, normalize ONLY this field;
    # all other fields must still pass schema/dependency validation.
    if not isinstance(content, dict):
        raise ValueError("Codex plan output must be a JSON object")
    proposed_project = content.get("project")
    if proposed_project != cfg.project:
        print(f"Warning: Codex returned project={proposed_project!r}; "
              f"using configured Jira project {cfg.project!r} instead.", flush=True)
        content["project"] = cfg.project
    validate_plan(content, cfg.project)
    write_plan(output_path, content)
    print(f"Plan generated: {output_path}\nReview and edit this file before apply-plan.")


def apply_plan(cfg: Config, plan_path: Path) -> None:
    plan = read_plan(plan_path)
    validate_plan(plan, cfg.project)
    state_file = plan_path.with_name(plan_path.stem + ".applied.json")
    state = json.loads(state_file.read_text()) if state_file.exists() else {"issues": {}, "links": []}
    jira = Jira(cfg)

    def persist() -> None:
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(state_file)

    def description(item: dict, epic: dict) -> str:
        criteria = "\n".join(f"- {value}" for value in item["acceptance_criteria"])
        # Jira's priority names depend on space configuration: don't blindly set.
        return (f"{item['description']}\n\n"
                f"Phase: {epic.get('phase', 'unspecified')}\n"
                f"Suggested priority: {item.get('priority', 'unspecified')}\n\n"
                f"Acceptance criteria:\n{criteria}")

    for epic in plan["epics"]:
        if epic["id"] not in state["issues"]:
            desc = f"Phase: {epic.get('phase', 'unspecified')}\n\n{epic['description']}"
            key = jira.create_issue(epic["summary"], desc, "Epic")
            state["issues"][epic["id"]] = key
            persist()
            print(f"Epic {epic['id']} -> {key}")
        epic_key = state["issues"][epic["id"]]
        for task in epic["tasks"]:
            if task["id"] not in state["issues"]:
                issue_type = task.get("type", "Task")
                key = jira.create_issue(task["summary"], description(task, epic),
                                        issue_type, parent=epic_key)
                state["issues"][task["id"]] = key
                persist()
                print(f"{issue_type} {task['id']} -> {key}")
            parent_key = state["issues"][task["id"]]
            for child in task.get("subtasks", []):
                if child["id"] not in state["issues"]:
                    key = jira.create_issue(child["summary"], description(child, epic),
                                            "Subtask", parent=parent_key)
                    state["issues"][child["id"]] = key
                    persist()
                    print(f"Subtask {child['id']} -> {key}")
    for epic in plan["epics"]:
        for task in epic["tasks"]:
            for item in [task, *task.get("subtasks", [])]:
                for dep in item["depends_on"]:
                    pair = f"{dep}>{item['id']}"
                    if pair not in state["links"]:
                        jira.blocks(state["issues"][dep], state["issues"][item["id"]])
                        state["links"].append(pair)
                        persist()
                        print(f"Dependency {pair}")
    print(f"Created/reconciled tickets; mapping: {state_file}")
    print("New tickets are unapproved: human must set Ready AND codex-ready.")

def audit_dependencies(cfg: Config, plan_path: Path, fix: bool = False) -> None:
    """Audit/repair *only* dependency pairs declared in plan.yaml.

    This command does not reconstruct or delete unrelated Jira links. Any
    deletion requires explicit --fix --yes and a link ID returned by Jira.
    All operations are repeatable after an interruption.
    """
    plan = read_plan(plan_path)
    validate_plan(plan, cfg.project)
    state_file = plan_path.with_name(plan_path.stem + ".applied.json")
    if not state_file.exists():
        raise ValueError(f"Missing {state_file}; cannot map plan IDs to Jira keys")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    issues = state.get("issues", {})
    jira = Jira(cfg)
    expected = []
    for epic in plan["epics"]:
        for task in epic["tasks"]:
            for item in [task, *task.get("subtasks", [])]:
                for dep in item["depends_on"]:
                    if dep not in issues or item["id"] not in issues:
                        raise ValueError(f"Missing applied Jira mapping for {dep} -> {item['id']}")
                    expected.append((dep, item["id"], issues[dep], issues[item["id"]]))

    if not expected:
        print("No plan dependencies to audit.")
        return

    # Indexing by the blocked endpoint keeps read traffic modest and inspects
    # both link directions without relying on the cached plan.applied.json links.
    cache = {}
    problems = 0
    for dep_id, item_id, blocker, blocked in expected:
        if blocked not in cache:
            cache[blocked] = jira.issue(blocked)
        links = cache[blocked].get("fields", {}).get("issuelinks", [])
        correct = [link for link in links
                   if link.get("type", {}).get("name", "").casefold() == "blocks"
                   and link.get("inwardIssue", {}).get("key") == blocker]
        reversed_links = [link for link in links
                          if link.get("type", {}).get("name", "").casefold() == "blocks"
                          and link.get("outwardIssue", {}).get("key") == blocker]
        status = "OK" if correct and not reversed_links else (
            "BOTH" if correct and reversed_links else
            "REVERSED" if reversed_links else "MISSING")
        print(f"{dep_id} ({blocker}) blocks {item_id} ({blocked}): {status}")
        if status == "OK":
            continue
        problems += 1
        if not fix:
            continue
        # A missing link ID must not trigger a guessed DELETE request.
        for link in reversed_links:
            if not link.get("id"):
                raise ValueError(f"Cannot delete reversed {blocked}/{blocker} link without Jira link ID")
        for link in reversed_links:
            jira.remove_link(str(link["id"]))
            print(f"  Deleted reversed Jira link {link['id']}")
        if not correct:
            jira.blocks(blocker, blocked)
            print(f"  Created correct link: {blocker} blocks {blocked}")
        # Re-fetch after mutation so subsequent dependency pairs see fresh links.
        cache.pop(blocked, None)
    if problems and not fix:
        print(f"Found {problems} incorrect/missing dependencies. To repair, review this output, "
              f"then run: python3 orchestrator.py audit-dependencies {plan_path} --fix --yes")
    elif fix:
        print(f"Dependency repair complete ({problems} pair(s) corrected).")
    else:
        print("All declared Jira dependencies match the YAML plan.")


def ensure_git_repo(cfg: Config) -> None:
    actual = Path(run_command(["git", "-C", str(cfg.repo), "rev-parse", "--show-toplevel"])).resolve()
    if actual != cfg.repo:
        raise ValueError(f"REPO_PATH must be Git repository root: {actual}")


# Recovery journal: one state file per Jira issue. All writes happen under the host lock.
# Stages are *next actions*, which are designed to be repeatable after a process crash.
STAGES = {"prepare", "codex", "verify", "commit", "push", "pr", "jira", "cleanup", "complete", "manual"}
QUOTA_PATTERNS = (
    r"(?:you(?:'|’)?ve\s+hit|reached|exceeded)\s+(?:your\s+)?(?:codex\s+)?(?:usage|rate|weekly|quota)\s+limit",
    r"(?:usage|rate|weekly|quota)\s+limit\s+(?:reached|exceeded)",
    r"(?:insufficient_quota|usage_limit_reached|rate_limit_exceeded|quota_exceeded)",
    r"(?:too many requests|429).*?(?:quota|codex|rate|usage)",
    r"(?:rate limit exceeded|out of credits|not enough credits)",
)
PERMANENT_PATTERNS = (
    r"(?:401|403).*?(?:unauthorized|forbidden|permission)",
    r"(?:authentication failed|permission denied|repository not found)",
    r"(?:invalid api token|not logged in|please run codex login)",
    r"(?:unsafe|refusing to commit|no jira transition|no file changes)",
)


class WorkerError(RuntimeError):
    def __init__(self, message: str, kind: str = "transient"):
        super().__init__(message)
        self.kind = kind


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def instant() -> str:
    return utcnow().isoformat(timespec="seconds")


def valid_issue_key(key: str, cfg: Config) -> bool:
    return bool(re.fullmatch(re.escape(cfg.project) + r"-[1-9][0-9]*", key))


def state_folder(cfg: Config) -> Path:
    directory = cfg.worktree_root / "state"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def state_path(cfg: Config, key: str) -> Path:
    if not valid_issue_key(key, cfg):
        raise ValueError(f"Invalid issue key for project {cfg.project}: {key!r}")
    return state_folder(cfg) / f"{key}.json"


def persist_job(cfg: Config, job: dict) -> None:
    """Atomically checkpoint state, including after Codex announces its thread ID."""
    path = state_path(cfg, job["key"])
    job["updated_at"] = instant()
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(job, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_job(cfg: Config, key: str) -> dict | None:
    path = state_path(cfg, key)
    if not path.exists():
        return None
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("key") != key or job.get("stage") not in STAGES or job.get("version") != 1:
        raise WorkerError(f"Invalid recovery state at {path}", "manual")
    if job.get("repo") != str(cfg.repo) or job.get("base_branch") != cfg.base_branch:
        raise WorkerError(f"Repository/base changed since {key} began; inspect {path}", "manual")
    return job


def new_job(cfg: Config, issue: dict, *, adopted: bool = False) -> dict:
    key = issue["key"]
    if not valid_issue_key(key, cfg):
        raise ValueError(f"Unexpected Jira key {key!r}")
    return {
        "version": 1, "key": key, "stage": "codex" if adopted else "prepare",
        "repo": str(cfg.repo), "base_branch": cfg.base_branch,
        "branch": f"codex/{key}", "worktree": str(cfg.worktree_root / key),
        "session_id": None, "attempts": 0, "codex_attempts": 0,
        "codex_complete": False, "active_pid": None, "active_kind": None,
        "verify_repairs": 0, "next_retry_at": None,
        "failure_kind": None, "last_error": None,
        "pr_url": None, "comment_marker": None,
        "repair_context": None, "adopted": adopted,
        "started_at": instant(), "updated_at": instant(),
    }


def pause_due(job: dict) -> bool:
    timestamp = job.get("next_retry_at")
    return not timestamp or utcnow() >= datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def backoff_seconds(kind: str, attempts: int, cfg: Config) -> int:
    if kind == "quota":
        return cfg.quota_retry_seconds
    return min(cfg.retry_base_seconds * (2 ** max(0, attempts - 1)), 3600)


def classify_failure(message: str) -> str:
    if any(re.search(p, message, flags=re.I | re.S) for p in QUOTA_PATTERNS):
        return "quota"
    if any(re.search(p, message, flags=re.I | re.S) for p in PERMANENT_PATTERNS):
        return "manual"
    return "transient"


def schedule_failure(cfg: Config, job: dict, exc: Exception) -> None:
    category = exc.kind if isinstance(exc, WorkerError) else classify_failure(str(exc))
    job["attempts"] += 1
    job["failure_kind"] = category
    job["last_error"] = str(exc)[-2500:]
    if category == "manual" or (category != "quota" and job["attempts"] >= cfg.max_retries):
        job["resume_stage"] = job["stage"]
        job["stage"] = "manual"
        job["next_retry_at"] = None
        print(f"{job['key']} needs human intervention: {exc}", file=sys.stderr)
    else:
        delay = backoff_seconds(category, job["attempts"], cfg)
        job["next_retry_at"] = (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        print(f"{job['key']} paused ({category}), retry no sooner than {job['next_retry_at']}", file=sys.stderr)
    persist_job(cfg, job)


def advance(cfg: Config, job: dict, next_stage: str) -> None:
    job["stage"] = next_stage
    job["failure_kind"] = None
    job["last_error"] = None
    job["next_retry_at"] = None
    job["attempts"] = 0
    persist_job(cfg, job)


def issue_status(issue: dict) -> str:
    return issue.get("fields", {}).get("status", {}).get("name", "")


def require_authorization(cfg: Config, issue: dict, job: dict) -> None:
    current = issue_status(issue).casefold()
    if current == cfg.review_status.casefold() and job["stage"] in {"jira", "cleanup", "complete"}:
        return
    if current == "done" or issue.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key") == "done":
        raise WorkerError("Ticket is Done; refusing further automation", "manual")
    if job["stage"] in {"prepare", "codex", "verify", "commit", "push", "pr"}:
        if cfg.ready_label not in issue.get("fields", {}).get("labels", []):
            raise WorkerError(f"Authorization label {cfg.ready_label} was removed", "manual")
        allowed = {cfg.ready_status.casefold(), cfg.in_progress_status.casefold()}
        if current and current not in allowed:
            raise WorkerError(f"Unexpected Jira status {issue_status(issue)!r}; refusing automation", "manual")


def ensure_worktree(cfg: Config, job: dict) -> None:
    worktree = Path(job["worktree"])
    branch = job["branch"]
    if worktree.exists():
        actual = run_command(["git", "-C", str(worktree), "branch", "--show-current"])
        toplevel = Path(run_command(["git", "-C", str(worktree), "rev-parse", "--show-toplevel"]))
        if actual != branch or toplevel.resolve() != worktree.resolve():
            raise WorkerError(f"Worktree {worktree} does not match expected branch {branch}", "manual")
        return
    run_command(["git", "-C", str(cfg.repo), "fetch", "origin", cfg.base_branch], timeout=180)
    # A branch without its worktree is suspicious; never reset or overwrite it.
    existing = subprocess.run(["git", "-C", str(cfg.repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        raise WorkerError(f"Branch {branch} exists without its saved worktree; inspect manually", "manual")
    run_command(["git", "-C", str(cfg.repo), "worktree", "add", "-b", branch,
                 str(worktree), f"origin/{cfg.base_branch}"])


def terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=4)


def run_codex_stream(cfg: Config, job: dict, prompt: str, log: Path, last_message: Path) -> None:
    """Run Codex and journal raw events, while displaying safe high-level progress.

    The terminal intentionally does not print model text, command arguments or file
    contents: these may contain private source data. The full local log is restricted.
    """
    worktree = Path(job["worktree"])
    args = codex_args(worktree) + ["--json", "--output-last-message", str(last_message)]
    is_resume = bool(job.get("session_id"))
    if is_resume:
        args += ["resume", job["session_id"], prompt]
    else:
        args.append(prompt)
    print(f"{job['key']}: {'resuming' if is_resume else 'starting'} Codex in {worktree}", flush=True)
    print(f"{job['key']}: execution log: {log} (watch: tail -f {log})", flush=True)
    proc = subprocess.Popen(args, cwd=worktree, env=safe_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    job["active_pid"] = proc.pid
    job["active_kind"] = "codex"
    persist_job(cfg, job)
    deadline = time.monotonic() + cfg.codex_timeout
    start = time.monotonic()
    last_heartbeat = start
    last_event_at = start
    event_count = 0
    last_event_type = "none"
    tail = bytearray()
    buffered = bytearray()
    failed_event = False
    try:
        selector = selectors.DefaultSelector()
        with selector, log.open("ab") as output:
            output.write(f"\n--- CODEX {instant()} ---\n".encode())
            output.flush()
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                now = time.monotonic()
                if now > deadline:
                    raise WorkerError(f"Codex timeout after {cfg.codex_timeout}s; logs: {log}", "transient")
                if now - last_heartbeat >= 15:
                    print(f"{job['key']}: Codex running {int(now - start)}s; "
                          f"events={event_count}, last={last_event_type} "
                          f"({int(now - last_event_at)}s ago)", flush=True)
                    last_heartbeat = now
                for _key, _mask in selector.select(timeout=0.3):
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(proc.stdout)
                        continue
                    output.write(chunk)
                    output.flush()
                    tail.extend(chunk)
                    if len(tail) > 16000:
                        del tail[:-16000]
                    buffered.extend(chunk)
                    if len(buffered) > 1000000:
                        buffered = buffered[-16000:]
                    while b"\n" in buffered:
                        raw, _, remainder = buffered.partition(b"\n")
                        buffered = bytearray(remainder)
                        try:
                            event = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        event_type = event.get("type")
                        if not isinstance(event_type, str):
                            continue
                        event_count += 1
                        last_event_type = event_type
                        last_event_at = time.monotonic()
                        if event_type == "thread.started" and event.get("thread_id"):
                            new_id = event["thread_id"]
                            if job.get("session_id") != new_id:
                                job["session_id"] = new_id
                                persist_job(cfg, job)
                            print(f"{job['key']}: Codex session recorded", flush=True)
                        elif event_type in {"turn.started", "turn.completed", "turn.failed", "error"}:
                            print(f"{job['key']}: Codex {event_type}", flush=True)
                        elif event_type in {"item.started", "item.completed", "item.updated"}:
                            item_type = event.get("item", {}).get("type") if isinstance(event.get("item"), dict) else None
                            # Show safe item categories; do not expose command/text/source content.
                            if item_type in {"command_execution", "file_change"} and event_type != "item.updated":
                                print(f"{job['key']}: Codex {event_type} ({item_type})", flush=True)
                        if event_type == "turn.completed":
                            job["codex_complete"] = True
                            persist_job(cfg, job)
                        if event_type in {"turn.failed", "error"}:
                            failed_event = True
                if proc.poll() is not None and not selector.get_map():
                    break
            output.flush()
        output_text = tail.decode("utf-8", errors="replace")
        if proc.wait() != 0 or failed_event:
            job["codex_complete"] = False
            persist_job(cfg, job)
            category = classify_failure(output_text)
            raise WorkerError(f"Codex exited unsuccessfully ({proc.returncode}, event_failure={failed_event}). "
                              f"See {log}. Recent output: {output_text[-1400:]}", category)
        print(f"{job['key']}: Codex finished successfully ({int(time.monotonic() - start)}s)", flush=True)
    except BaseException:
        terminate_group(proc)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
        job["active_pid"] = None
        job["active_kind"] = None
        persist_job(cfg, job)


def verify_tests(cfg: Config, job: dict, log: Path) -> None:
    process = subprocess.Popen(["bash", "-lc", cfg.check_command], cwd=job["worktree"],
                               env=safe_env(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    job["active_pid"] = process.pid
    job["active_kind"] = "verification"
    persist_job(cfg, job)
    try:
        output, _ = process.communicate(timeout=cfg.test_timeout)
    except BaseException:
        terminate_group(process)
        raise
    finally:
        job["active_pid"] = None
        job["active_kind"] = None
        persist_job(cfg, job)
    with log.open("ab") as stream:
        stream.write(f"\n--- VERIFICATION {instant()} ---\n".encode() + output[-200000:])
    if process.returncode:
        context = output.decode("utf-8", errors="replace")[-6000:]
        job["verify_repairs"] += 1
        job["repair_context"] = context
        if job["verify_repairs"] > cfg.max_test_repairs:
            raise WorkerError(f"Verification failed {job['verify_repairs']} times; inspect {log}", "manual")
        job["stage"] = "codex"
        job["codex_complete"] = False
        persist_job(cfg, job)
        raise WorkerError(f"Verification failed; Codex will attempt repair. See {log}", "transient")


def git_commits_ahead(cfg: Config, job: dict) -> list[str]:
    output = run_command(["git", "-C", job["worktree"], "log", "--format=%s", f"origin/{cfg.base_branch}..HEAD"])
    return output.splitlines() if output else []


def commit_if_needed(cfg: Config, job: dict, title: str) -> None:
    worktree = job["worktree"]
    ahead = git_commits_ahead(cfg, job)
    if ahead:
        if len(ahead) != 1 or not ahead[0].startswith(f"{job['key']}:"):
            raise WorkerError(f"Unexpected commits on {job['branch']}: {ahead}; inspect manually", "manual")
        status = run_command(["git", "-C", worktree, "status", "--porcelain"])
        if status:
            raise WorkerError("Commits exist but worktree has additional changes; inspect manually", "manual")
        return
    run_command(["git", "-C", worktree, "add", "-A"])
    staged = run_command(["git", "-C", worktree, "diff", "--cached", "--name-only"])
    if not staged:
        raise WorkerError("Codex completed but produced no file changes", "manual")
    forbidden = [name for name in staged.splitlines() if re.search(
        r"(^|/)(\.env($|\.)|id_rsa$|id_ed25519$)|\.(pem|p12|pfx|key)$", name, re.I)]
    if forbidden:
        raise WorkerError(f"Refusing to commit possible secrets: {forbidden}", "manual")
    run_command(["git", "-C", worktree, "commit", "-m", f"{job['key']}: {title[:70]}"])


def existing_pr(cfg: Config, job: dict) -> str | None:
    data = run_command(["gh", "pr", "list", "--head", job["branch"], "--state", "all",
                        "--json", "url,state", "--limit", "100"], cwd=Path(job["worktree"]), timeout=120)
    prs = json.loads(data)
    if len(prs) > 1:
        raise WorkerError(f"Multiple PRs for branch {job['branch']}; review manually", "manual")
    if not prs:
        return None
    if prs[0]["state"].upper() != "OPEN":
        raise WorkerError(f"PR is {prs[0]['state']}: {prs[0]['url']}; will not recreate", "manual")
    return prs[0]["url"]


def ensure_pr(cfg: Config, job: dict, title: str, last_message: Path) -> str:
    found = existing_pr(cfg, job)
    if found:
        return found
    summary = last_message.read_text(encoding="utf-8")[:3500] if last_message.exists() else ""
    issue_url = f"{cfg.url}/browse/{job['key']}"
    body = f"Jira issue: [{job['key']}]({issue_url})\n\nAutomated implementation; human review required.\n\n{summary}"
    result = run_command(["gh", "pr", "create", "--draft", "--base", cfg.base_branch,
                          "--head", job["branch"], "--title", f"{job['key']}: {title[:110]}",
                          "--body", body], cwd=Path(job["worktree"]), timeout=120)
    return result.splitlines()[-1]


def jira_finalize(cfg: Config, jira: Jira, job: dict) -> None:
    marker = f"codex-jira:{job['key']}:pr"
    if not jira.has_comment_marker(job["key"], marker):
        jira.comment(job["key"], f"[{marker}]\nCodex implementation ready for human review.\n"
                     f"Draft PR: {job['pr_url']}\nVerification: {cfg.check_command}")
    current = issue_status(jira.issue(job["key"]))
    if current.casefold() != cfg.review_status.casefold():
        jira.transition(job["key"], cfg.review_status)


def note_paused_job(cfg: Config, jira: Jira, job: dict) -> None:
    """Best-effort Jira visibility; failures never overwrite the local recovery journal."""
    if job.get("failure_kind") == "quota":
        marker = f"codex-jira:{job['key']}:quota"
        if not jira.has_comment_marker(job["key"], marker):
            jira.comment(job["key"], f"[{marker}]\nCodex usage limit reached. The local worker "
                         f"will retry no sooner than {job['next_retry_at']} and resume saved work.")
    elif job["stage"] == "manual":
        marker = f"codex-jira:{job['key']}:manual"
        if not jira.has_comment_marker(job["key"], marker):
            jira.comment(job["key"], f"[{marker}]\nWorker requires human intervention. "
                         f"Saved stage: {job.get('resume_stage')}.\n"
                         "Inspect the restricted local worker logs for error details.\n"
                         f"Use `python3 orchestrator.py status` and `recover {job['key']} --retry-now` "
                         "after investigating.")
        current = issue_status(jira.issue(job["key"]))
        if current.casefold() not in {cfg.blocked_status.casefold(), cfg.review_status.casefold(), "done"}:
            jira.transition(job["key"], cfg.blocked_status)


def safely_note_pause(cfg: Config, jira: Jira, job: dict) -> None:
    try:
        note_paused_job(cfg, jira, job)
    except Exception as exc:
        print(f"Could not annotate Jira for {job['key']}: {exc}", file=sys.stderr)


def run_saved_job(cfg: Config, jira: Jira, job: dict) -> None:
    key = job["key"]
    if job["stage"] in {"complete", "manual"}:
        print(f"{key}: state is {job['stage']}; no automatic work")
        return
    if not pause_due(job):
        print(f"{key}: {job['failure_kind']} pause until {job['next_retry_at']}")
        return
    issue = jira.issue(key)
    try:
        require_authorization(cfg, issue, job)
    except WorkerError as exc:
        schedule_failure(cfg, job, exc)
        safely_note_pause(cfg, jira, job)
        return
    old_pid = job.get("active_pid")
    if old_pid is not None:
        try:
            os.killpg(int(old_pid), 0)
        except ProcessLookupError:
            job["active_pid"] = None
            job["active_kind"] = None
            persist_job(cfg, job)
        else:
            schedule_failure(cfg, job, WorkerError(
                f"Previous {job.get('active_kind')} process group {old_pid} still exists. "
                "Refusing concurrent work on the same checkout.", "transient"))
            safely_note_pause(cfg, jira, job)
            return
    title = issue["fields"]["summary"]
    description = plain(issue["fields"].get("description"))
    root = cfg.worktree_root
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = logs / f"{key}.log"
    last_message = logs / f"{key}.last.md"
    prompt = f"""Implement Jira {key} in this repository.
Title: {title}
Description and acceptance criteria:\n{description[:30000]}
Jira: {cfg.url}/browse/{key}

Inspect existing files and git status before changing anything. Preserve any partial
changes from previous interrupted attempts; do not duplicate or erase completed work.
Follow AGENTS.md. Implement the smallest coherent change and add meaningful tests.
Do NOT commit, push, open a PR, contact Jira or modify files outside this workspace.
Never access or print secrets. If ambiguous, state blockers rather than guessing.
End with a concise summary, tests and outstanding risks.
"""
    print(f"{key}: worker picked up ticket; stage={job['stage']}", flush=True)
    try:
        while job["stage"] not in {"complete", "manual"}:
            phase = job["stage"]
            print(f"{key}: stage={phase}", flush=True)
            if phase == "prepare":
                current = issue_status(issue)
                if current.casefold() != cfg.in_progress_status.casefold():
                    jira.transition(key, cfg.in_progress_status)
                ensure_worktree(cfg, job)
                advance(cfg, job, "codex")
            elif phase == "codex":
                ensure_worktree(cfg, job)
                if job.get("codex_complete"):
                    advance(cfg, job, "verify")
                    continue
                job["codex_attempts"] += 1
                persist_job(cfg, job)
                continuation = prompt
                if job.get("session_id"):
                    continuation = (f"Continue working on Jira {key}. Inspect existing changes and finish acceptance criteria. "
                                    "Preserve previous work; do not commit, push, or open PRs.\n" +
                                    ("Fix these verification failures:\n" + job["repair_context"]
                                     if job.get("repair_context") else "Review status and resume unfinished implementation."))
                elif job["codex_attempts"] > 1 or job.get("adopted"):
                    continuation += "\nThis worktree contains earlier partial work; inspect it and continue, do not restart."
                    if job.get("repair_context"):
                        continuation += "\nFix these verification failures:\n" + job["repair_context"]
                run_codex_stream(cfg, job, continuation, log, last_message)
                job["repair_context"] = None
                advance(cfg, job, "verify")
            elif phase == "verify":
                verify_tests(cfg, job, log)
                advance(cfg, job, "commit")
            elif phase == "commit":
                commit_if_needed(cfg, job, title)
                advance(cfg, job, "push")
            elif phase == "push":
                run_command(["git", "-C", job["worktree"], "push", "-u", "origin", job["branch"]], timeout=180)
                advance(cfg, job, "pr")
            elif phase == "pr":
                job["pr_url"] = ensure_pr(cfg, job, title, last_message)
                advance(cfg, job, "jira")
            elif phase == "jira":
                if not job.get("pr_url"):
                    # Recover from a crash after PR creation but before state persistence.
                    job["pr_url"] = existing_pr(cfg, job)
                    if not job["pr_url"]:
                        raise WorkerError("Missing PR URL for Jira finalization", "manual")
                    persist_job(cfg, job)
                jira_finalize(cfg, jira, job)
                advance(cfg, job, "cleanup")
            elif phase == "cleanup":
                worktree = Path(job["worktree"])
                if worktree.exists():
                    # No --force: leave any unexpected modifications for investigation.
                    run_command(["git", "-C", str(cfg.repo), "worktree", "remove", str(worktree)])
                advance(cfg, job, "complete")
                print(f"Completed {key}: {job['pr_url']} (Jira in review)")
            else:
                raise WorkerError(f"Unknown stage {phase!r}", "manual")
    except Exception as exc:
        schedule_failure(cfg, job, exc)
        safely_note_pause(cfg, jira, job)
    except (KeyboardInterrupt, SystemExit):
        # The already-persisted stage is safe for the next invocation to replay.
        persist_job(cfg, job)
        raise


def all_jobs(cfg: Config) -> list[dict]:
    result = []
    for path in sorted(state_folder(cfg).glob("*.json")):
        if not valid_issue_key(path.stem, cfg):
            continue
        result.append(load_job(cfg, path.stem))
    return result


def show_jobs(cfg: Config) -> None:
    jobs = all_jobs(cfg)
    if not jobs:
        print("No recorded worker jobs")
    for job in jobs:
        suffix = f", retry={job['next_retry_at']}" if job.get("next_retry_at") else ""
        print(f"{job['key']}: stage={job['stage']}, failure={job.get('failure_kind')}, "
              f"session={job.get('session_id') or '-'}, attempts={job['attempts']}{suffix}")


def recover_job(cfg: Config, key: str, *, retry_now: bool = False, adopt_worktree: bool = False) -> None:
    with single_worker_lock(cfg.worktree_root):
        jira = Jira(cfg)
        issue = jira.issue(key)
        job = load_job(cfg, key)
        if adopt_worktree:
            if job:
                raise ValueError(f"{key} already has persisted state; omit --adopt-worktree")
            if cfg.ready_label not in issue.get("fields", {}).get("labels", []):
                raise ValueError(f"Add {cfg.ready_label} label before adopting")
            job = new_job(cfg, issue, adopted=True)
            worktree = Path(job["worktree"])
            if not worktree.exists():
                raise ValueError(f"No old worktree at {worktree}")
            ensure_worktree(cfg, job)
            if issue_status(issue).casefold() == cfg.blocked_status.casefold():
                jira.transition(key, cfg.in_progress_status)
            persist_job(cfg, job)
            print(f"Adopted {key}; first run will inspect existing partial source changes")
        elif not job:
            raise ValueError(f"No saved job for {key}; use --adopt-worktree only for an old worker checkout")
        if retry_now:
            if job["stage"] == "complete":
                raise ValueError("Completed job cannot be retried")
            if job["stage"] == "manual":
                job["stage"] = job.pop("resume_stage", "codex")
                if job["stage"] in {"codex", "verify", "commit", "push", "pr"} and \
                   issue_status(issue).casefold() == cfg.blocked_status.casefold():
                    jira.transition(key, cfg.in_progress_status)
            job["attempts"] = 0
            job["next_retry_at"] = None
            job["failure_kind"] = None
            persist_job(cfg, job)
            print(f"Retry authorized: {key} at stage {job['stage']}")
        run_saved_job(cfg, jira, job)


@contextlib.contextmanager
def single_worker_lock(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A worker is already running on this machine") from exc
        yield


def once(cfg: Config, dry_run: bool = False, already_locked: bool = False) -> None:
    """Resume an interrupted job first, otherwise claim one authorized Ready task."""
    ensure_git_repo(cfg)
    jira = Jira(cfg)
    lock = contextlib.nullcontext() if already_locked else single_worker_lock(cfg.worktree_root)
    with lock:
        jobs = all_jobs(cfg)
        # Quota exhaustion is account-wide. Do not start new tickets while waiting.
        quota_jobs = [job for job in jobs if job.get("failure_kind") == "quota"
                      and job["stage"] not in {"manual", "complete"}]
        if quota_jobs:
            job = sorted(quota_jobs, key=lambda x: x["updated_at"])[0]
            if dry_run:
                print(f"DRY RUN: quota-paused {job['key']}, next retry {job['next_retry_at']}")
                return
            run_saved_job(cfg, jira, job)
            return
        pending = [job for job in jobs if job["stage"] not in {"manual", "complete"}]
        due = [job for job in pending if pause_due(job)]
        if due:
            job = sorted(due, key=lambda x: x["updated_at"])[0]
            if dry_run:
                print(f"DRY RUN: would resume {job['key']} at stage {job['stage']}")
                return
            run_saved_job(cfg, jira, job)
            return
        candidates = jira.ready_issues()
        if not candidates:
            print("No Ready + codex-ready tickets; no due recovery jobs")
            return
        for candidate in candidates:
            key = candidate["key"]
            if load_job(cfg, key):
                continue  # Never run a recorded job as a new ticket.
            issue = jira.issue(key)
            fields = issue["fields"]
            if fields.get("issuetype", {}).get("name", "").casefold() not in {
                "task", "story", "bug", "sub-task", "subtask"
            }:
                continue
            if fields.get("subtasks"):
                print(f"Skipping {key}: parent has subtasks")
                continue
            if not jira.dependencies_satisfied(issue):
                print(f"Skipping {key}: blocked by dependency")
                continue
            if dry_run:
                print(f"DRY RUN: would implement {key}: {fields['summary']}")
                return
            job = new_job(cfg, issue)
            if Path(job["worktree"]).exists():
                raise WorkerError(f"Existing untracked worktree {job['worktree']}; "
                                  f"use recover {key} --adopt-worktree explicitly", "manual")
            persist_job(cfg, job)  # Checkpoint before changing Jira or Git.
            run_saved_job(cfg, jira, job)
            return
        if pending:
            print("No recovery job due yet; no other unclaimed executable ticket")
        else:
            print("No unblocked executable tickets in first 50 results")

def bootstrap(cfg: Config, brief_path: Path, plan_path: Path) -> None:
    """Generate plan if absent and sync it to Jira, without authorization."""
    ensure_git_repo(cfg)
    if not plan_path.exists():
        plan_command(cfg, brief_path, plan_path)
    else:
        print(f"Reusing plan: {plan_path}")
    plan = read_plan(plan_path)
    validate_plan(plan, cfg.project)
    invalid = [epic["id"] for epic in plan["epics"] if epic.get("phase") not in {"P0", "P1"}]
    if invalid:
        raise ValueError(f"Bootstrap refuses Epics outside P0/P1 or without a phase: {invalid}")
    apply_plan(cfg, plan_path)

def main() -> None:
    os.umask(0o077)
    # Graceful SIGTERM lets Codex/test process groups stop, preserving checkpoints.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan", help="Generate P0/P1 plan YAML; does not write Jira")
    plan_parser.add_argument("brief", type=Path)
    plan_parser.add_argument("--out", type=Path, default=ROOT / "plan.yaml")
    plan_parser.add_argument("--log", type=Path, default=ROOT / "logs" / "plan-generation.jsonl",
                             help="Persistent Codex JSON event log")
    apply_parser = sub.add_parser("apply-plan", help="Idempotently create Jira tickets/links")
    apply_parser.add_argument("plan", type=Path)
    apply_parser.add_argument("--yes", action="store_true")
    audit_parser = sub.add_parser("audit-dependencies", help="Inspect existing Jira Blocks links versus plan.yaml")
    audit_parser.add_argument("plan", type=Path)
    audit_parser.add_argument("--fix", action="store_true", help="Delete reversed links and create correct ones")
    audit_parser.add_argument("--yes", action="store_true", help="Confirm Jira link repair")
    validate_parser = sub.add_parser("validate-plan", help="Validate plan without Jira changes")
    validate_parser.add_argument("plan", type=Path)
    once_parser = sub.add_parser("once", help="Execute one approved Jira ticket")
    once_parser.add_argument("--dry-run", action="store_true")
    boot_parser = sub.add_parser("bootstrap", help="Generate plan if missing and create unapproved tickets")
    boot_parser.add_argument("--brief", type=Path, default=ROOT / "project_brief.md")
    boot_parser.add_argument("--plan", type=Path, default=ROOT / "plan.yaml")
    boot_parser.add_argument("--yes", action="store_true", help="Authorize Jira issue creation")
    tick_parser = sub.add_parser("tick", help="Optional bootstrap, then implement one approved ticket")
    tick_parser.add_argument("--bootstrap", action="store_true", help="Allow generating and applying tickets")
    tick_parser.add_argument("--brief", type=Path, default=ROOT / "project_brief.md")
    tick_parser.add_argument("--plan", type=Path, default=ROOT / "plan.yaml")
    tick_parser.add_argument("--dry-run", action="store_true", help="Read-only; never bootstrap")
    tick_parser.add_argument("--review", action="store_true", help="Run one AI review/fix step after regular ticket work")
    sub.add_parser("status", help="Show durable execution checkpoints and retry times")
    review_parser = sub.add_parser("review-once", help="Process one PR review/fix checkpoint (opt-in)")
    review_parser.add_argument("--dry-run", action="store_true")
    run_parser = sub.add_parser("review-run", help="Continuously advance one PR review through fixes, verification and CI")
    run_parser.add_argument("--issue-key", help="Run a specific Jira ticket, e.g. SSG-8")
    run_parser.add_argument("--max-minutes", type=int, help="Overall runner time budget; default REVIEW_RUN_MAX_MINUTES")
    run_parser.add_argument("--dry-run", action="store_true", help="Show selected PR without executing it")
    sub.add_parser("review-status", help="Show independent AI-review checkpoints")
    resume_parser = sub.add_parser("review-resume", help="Re-review a budget-exhausted PR after a human pushes a new commit")
    resume_parser.add_argument("issue_key", help="Jira key, e.g. SSG-8")
    review_retry = sub.add_parser("review-retry", help="Retry a paused review checkpoint")
    review_retry.add_argument("issue_key")
    recover_parser = sub.add_parser("recover", help="Resume a paused job or adopt a pre-upgrade worktree")
    recover_parser.add_argument("issue_key", help="Jira key, e.g. AIDEV-12")
    recover_parser.add_argument("--retry-now", action="store_true", help="Clear cooldown or manual stop")
    recover_parser.add_argument("--adopt-worktree", action="store_true", help="Explicitly adopt old MVP partial worktree")
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
        plan_command(cfg, args.brief, args.out, log_path=args.log)
    elif args.command == "apply-plan":
        if not args.yes:
            parser.error("apply-plan writes Jira tickets; add --yes after review")
        with single_worker_lock(cfg.worktree_root):
            apply_plan(cfg, args.plan)
    elif args.command == "audit-dependencies":
        if args.fix and not args.yes:
            parser.error("Repair changes Jira links; add --yes after reviewing audit output")
        if args.fix:
            with single_worker_lock(cfg.worktree_root):
                audit_dependencies(cfg, args.plan, fix=True)
        else:
            audit_dependencies(cfg, args.plan, fix=False)
    elif args.command == "once":
        once(cfg, args.dry_run)
    elif args.command == "bootstrap":
        if not args.yes:
            parser.error("bootstrap writes Jira tickets; add --yes after choosing this mode")
        with single_worker_lock(cfg.worktree_root):
            bootstrap(cfg, args.brief, args.plan)
    elif args.command == "status":
        show_jobs(cfg)
    elif args.command == "review-once":
        import review_worker
        review_worker.review_once(cfg, dry_run=args.dry_run)
    elif args.command == "review-run":
        import review_worker
        review_worker.review_run(cfg, issue_key=args.issue_key, max_minutes=args.max_minutes,
                                 dry_run=args.dry_run)
    elif args.command == "review-status":
        import review_worker
        review_worker.review_status(cfg)
    elif args.command == "review-resume":
        import review_worker
        review_worker.review_resume_after_human_push(cfg, args.issue_key)
    elif args.command == "review-retry":
        import review_worker
        review_worker.review_recover(cfg, args.issue_key)
    elif args.command == "recover":
        recover_job(cfg, args.issue_key, retry_now=args.retry_now, adopt_worktree=args.adopt_worktree)
    elif args.command == "tick":
        with single_worker_lock(cfg.worktree_root):
            if args.bootstrap and not args.dry_run:
                bootstrap(cfg, args.brief, args.plan)
            once(cfg, dry_run=args.dry_run, already_locked=True)
            if args.review:
                import review_worker
                review_worker.review_once(cfg, dry_run=args.dry_run, already_locked=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Durable job checkpoint is preserved for the next run.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
