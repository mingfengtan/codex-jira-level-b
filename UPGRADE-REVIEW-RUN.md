# Upgrade: continuous AI PR review runner

This update is **additive** and preserves saved review checkpoints; it does not
change your private repository, force-push, merge PRs, or change Jira to Done.

## Install

1. Stop any active scheduler, `tick --review`, or `review-once` process.
2. In your existing `codex-jira-level-b` scheduler directory:

   ```bash
   cp orchestrator.py orchestrator.py.before-review-run
   cp review_worker.py review_worker.py.before-review-run
   ```

3. Copy **both** updated `orchestrator.py` and `review_worker.py` from this
   package into that scheduler directory. **Do not copy `.env.example` over
   your `.env`.** Preserve `~/.codex-jira-worktrees/` and all state/logs.
4. Set `AUTO_REVIEW_ENABLED=true` in `.env`. Optionally append:

   ```dotenv
   REVIEW_RUN_MAX_MINUTES=90
   REVIEW_HEAD_POLL_SECONDS=3
   REVIEW_HEAD_WAIT_TIMEOUT=120
   REVIEW_CI_POLL_SECONDS=15
   REVIEW_CI_WAIT_TIMEOUT=900
   ```

5. Check the updated command and use it:

   ```bash
   python3 orchestrator.py review-run --help
   python3 orchestrator.py review-run --dry-run
   python3 orchestrator.py review-run
   ```

   To work on a specific PR:

   ```bash
   python3 orchestrator.py review-run --issue-key SSG-8
   ```

The command handles as much of **one PR** as possible in a single invocation:
Agent B review → bounded Agent A fixes → Flutter verification and bounded
repair → commit/push → PR HEAD sync → another review → GitHub CI → `passed`.
It stops for manual review if the configured fix budget is exhausted, if
unknown/environment verification fails, or if CI fails. It polls only GitHub
HEAD and CI when pending; it does not consume Codex calls while polling.
The time budget is *soft* for subprocesses already running. Every stage is
checkpointed so the next invocation can resume.

## SSG-8: automatic review fix budget exhausted

**Updated behavior:** you can now simply increase `MAX_AUTO_REVIEW_FIXES` in
`.env` (e.g. from `2` to `4`) and rerun
`python3 orchestrator.py review-run --issue-key SSG-8`.
The existing `manual` checkpoint automatically resumes at `fix_prepare` when
its error is **exactly** `Automatic review fix budget exhausted`, the saved
review requested changes, and `fix_rounds` is below the new limit. It does not
reset counters, regenerate Agent B's review, or override any other manual
failure. `review-resume` is **not** needed for this scenario.

The manual-fix/re-review procedure below remains available **if you choose to
fix the PR yourself instead of increasing the budget**.


Current checkpoint: `manual` with `Automatic review fix budget exhausted`.
Installing `review-run` alone will not restart Agent A at the exhausted
limit; increasing `MAX_AUTO_REVIEW_FIXES` above the used `fix_rounds` count
will now allow an automatic restart as described above. The latest Agent B findings are available on the PR and
in `~/.codex-jira-worktrees/review/SSG-8.json`.

Inspect:

```bash
python3 orchestrator.py review-status
cat ~/.codex-jira-worktrees/review/SSG-8.json
# From the application Git repo, inspect the PR and its review comments.
gh pr view --comments
```

After manually assessing and resolving the findings **and pushing a new commit
on that same PR branch**, run:

```bash
python3 orchestrator.py review-resume SSG-8
python3 orchestrator.py review-run --issue-key SSG-8
```

`review-resume` verifies that the PR HEAD changed from the last reviewed SHA;
it preserves both automatic fix rounds. A third Agent B review that still
requests changes goes back to `manual`, rather than giving Agent A another
unapproved fix round. Don't run `review-resume` before fixing/pushing code.

## Legacy commands

`once`, `review-once`, `tick --review`, `review-retry` and `review-status`
remain available. `tick --review` still processes one review stage and exits;
use `review-run` when you want a single continuous review execution.
