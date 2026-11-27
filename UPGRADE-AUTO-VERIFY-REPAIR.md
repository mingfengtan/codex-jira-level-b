# Upgrade: automatically repair initial implementation verification failures

This patch changes only `orchestrator.py`. The PR `review_worker.py` is unchanged.

## Installation

1. Stop any active scheduler invocation (only one worker should be running).
2. Back up your existing `orchestrator.py`.
3. Replace `orchestrator.py` from this package and keep your `.env`, review worker, worktrees, and checkpoint JSON files.
4. Configure a bounded repair budget in the scheduler's `.env`:

   ```dotenv
   MAX_TEST_REPAIRS=4
   ```

   `MAX_TEST_REPAIRS` counts additional Codex repair attempts in the **initial Jira ticket implementation**. A first failing verification triggers repair #1. If the last authorized repair still fails, the ticket stops for human intervention. This setting is separate from `MAX_REVIEW_TEST_REPAIRS` and `MAX_AUTO_REVIEW_FIXES`.

5. Check that your `.env` value is actually loaded (exported environment variables can override `.env`):

   ```bash
   python3 -c 'import orchestrator; orchestrator.load_env(); print(orchestrator.Config.from_env().max_test_repairs)'
   ```

6. Run the normal command:

   ```bash
   python3 orchestrator.py once
   ```

   Or use `python3 orchestrator.py tick` / `tick --review` as before.

## New behavior

```text
codex -> verify
          |  \\ tests pass -> commit -> push -> PR -> Jira -> complete
          |  \\ tests fail -> preserve test output -> codex repair -> verify
          |                   (repeat up to MAX_TEST_REPAIRS)
          |                   (if exhausted -> manual, never commit failing work)
```

Unlike the previous version, a code/test verification failure does not raise a transient error that forces `once` to exit before Agent A repairs it. It saves the test output and immediately resumes Codex in the same invocation. Other failures (e.g., a Codex quota error, infrastructure exception, or a user interrupt) still save a checkpoint and can pause safely. No cron or repeated manual command is needed for the unit-test repair *inside an active run*.

Existing saved transient `Verification failed; Codex will attempt repair` checkpoints are recognized; the next `once` automatically clears their old cooldown and resumes. If a job previously stopped in `manual` **because its verification repair limit was exhausted**, increasing `MAX_TEST_REPAIRS` above the recorded `verify_repairs` count lets the next `once` resume it (subject to Jira authorization, and if not Done). Other manual stops stay protected.

## SSG-10

1. Confirm `MAX_TEST_REPAIRS` is high enough for the existing `verify_repairs` counter. Your prior state showed `verify_repairs=2`. If you have had another failure, you may need to raise the configured limit.
2. Do not delete `~/.codex-jira-worktrees/state/SSG-10.json` or its worktree.
3. Run `python3 orchestrator.py once` to resume SSG-10 and let it repair/test continuously.
4. Follow the log via `tail -f ~/.codex-jira-worktrees/logs/SSG-10.log`.

The patch controls the retry workflow; it cannot guarantee Codex finds the root cause. With recurring failures the bounded budget will still stop for human review.
