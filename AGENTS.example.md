# AGENTS.md — Orbit Shot Flutter game (copy to APPLICATION repo root)

## Scope and delivery
- Work on ONE Jira ticket at a time; follow acceptance criteria, blockers and dependencies.
- In the first planning batch, implement P0/P1 only. Never start P2/P3/P4 monetization, remote content, live ads, multiplayer or production releases without explicit owner approval.
- If ambiguous, document the decision and block only when it materially affects correctness/security. Never silently change the economy, purchases or product scope.

## Architecture
- Flutter + Flame for the game app; separate pure Dart simulation, scoring, persistence, and level validation from rendering/widgets.
- Use fixed-step simulation; deterministic fixtures and clear numerical tolerances for collisions/gravity.
- Author levels as versioned YAML; validate before bundling. No backend or login for offline MVP.
- Use Flutter/Dart/CI tooling. Do not add Bazel, infrastructure, new paid services or copyrighted assets without approval.

## Quality
- Add/adjust domain unit tests and Flutter widget tests for every changed behavior.
- Run `dart format`, `flutter analyze`, `flutter test`, and relevant integration tests where supported.
- Add regression fixtures for physics changes and note real-device performance measurements when available.
- Keep accessibility labels, small/mobile screens and macOS mouse controls in mind.

## Git / Jira safety
- Do not commit, push, open PRs, merge, deploy, call Jira or access credentials: orchestrator handles GitHub/Jira after verification.
- Commit messages and PR titles from orchestrator must carry the Jira key. One task → one focused PR.
- Never mark Jira Done, approve your own ticket, or add `codex-ready` to Jira.
- Never include `.env`, tokens, certificates, personal data or secrets in tracked files.
- Keep final agent summary concise: changed files, tests, assumptions, blockers and risks.
