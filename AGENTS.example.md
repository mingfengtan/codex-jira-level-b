# Development conventions for this repository

- Implement only the scope and acceptance criteria of the current Jira ticket.
- Read relevant tests and surrounding code before making changes.
- Add or update automated tests for changed behavior.
- Prefer minimal, reviewable changes. Avoid unrelated refactors.
- Do not edit deployment or infrastructure credentials.
- Never include secrets or tokens in source files, logs, or PR comments.
- Document necessary follow-up work in the final summary. Do not silently expand scope.
- Do not mark Jira tickets Done or merge PRs; the human reviewer handles acceptance.
- If requirements are ambiguous, report the blocker instead of inventing product decisions.
