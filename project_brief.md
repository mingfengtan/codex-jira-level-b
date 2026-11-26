# Project Brief — Orbit Shot (working title)

Status: Product proposal for Jira planning; not an authorization to ship or spend money.
Product owner: Human reviewer. Project key: use `JIRA_PROJECT_KEY` in the orchestrator `.env` (not hard-coded here).

## 1. Product vision

Build an approachable, playful **2D physics-puzzle game** in which players position a ball on a bottom launch line, choose its launch direction, then fire it through obstacles and gravity fields to hit all required targets. Controls should take seconds to learn; progressively surprising physics and optional equipment should provide long-term interest. Players navigate a branching level map, earn up to three stars, collect coins, and unlock new ways to play.

Initial target platforms: Android, iOS, and macOS. The gameplay must work entirely offline, with no user account, multiplayer service, or custom backend for the first playable milestone. Do not introduce cloud services, SDK accounts, real purchases, or advertising without separate product-owner approval.

## 2. Delivery phases and authorization boundaries

- **P0 — Foundation (create actionable Jira tickets now):** Flutter project for Android/iOS/macOS; Flame integration; project structure; deterministic/testable physics core; basic rendering, drag-to-aim/launch controls; quality gates and documentation.
- **P1 — Playable vertical slice (create actionable Jira tickets now):** map screen, settings, ten authored levels of increasing difficulty, targets, walls/reflectors, win/fail/retry, coins, scores/stars, level unlocks, local persistence, and relevant automated tests. A minimal in-game shop using **earned** coins may be included only after core play works; no real-money payments.
- **P2 — Advanced gameplay (roadmap only; do not create implementation tickets unless explicitly requested):** gravity wells, anti-gravity, black holes, adjustable reflectors, equipment/jet balls, inventory and card slots, mystery boxes, larger level library and GUI level designer with simulation-based solution search.
- **P3 — Monetization (roadmap only):** in-app purchases for coin packs, subscriptions, ads, premium themes, monthly gifts and loot-box tiers. Requires separate product, legal/policy, purchase-validation, refund, age-rating, privacy and platform requirements. No mock checkout that can be mistaken for a real purchase.
- **P4 — Online/release (roadmap only):** competitive multiplayer (including m-v-n possibilities), server-authoritative currency/competitive scoring, cross-device entitlement rules, downloadable level packs, iOS/Android/macOS store release pipelines. These features may require a backend and should not compromise P0/P1 offline architecture.

The automatic plan generator must target **P0/P1 only**, ideally 3–5 Epics and 12–20 small, dependency-linked implementation tickets. Prefer one reviewable pull request per ticket. Do not implement every feature described in this brief in the first plan.

## 3. Technical architecture

- Flutter/Dart for cross-platform UI; Flame for 2D game rendering/input. Use **pure Dart domain objects** for physics, level rules, scoring, currency, and progression so their tests run without a device or rendering engine.
- Physics simulation should use fixed time steps and explicitly bounded forces to make levels replayable, simulations stable, and regression tests reproducible. Tune numerical tolerances for cross-device floating-point differences. Document collision and gravity conventions.
- Separate layers: `lib/domain` (simulation/rules), `lib/data` (level definitions and local persistence), `lib/game` (Flame components), `lib/ui` (Flutter navigation/settings), `test`, `integration_test`, and `tool` (validation, performance, release placeholders). Final layout may be refined during P0.
- Use `flutter pub`, `flutter analyze`, `flutter test`, and Flutter integration tests as the default workflow. **Do not introduce Bazel** unless it solves a demonstrated problem and the owner approves the additional maintenance burden.
- Author human-editable level files in YAML with schema/semantic validation. Bundle validated, versioned level data as local assets (YAML directly or generated JSON). A future level-pack format must include a format version; dynamic downloads are out of scope for P0/P1.
- Persist player settings, levels unlocked, best scores/stars, coin balance, and inventory locally behind a replaceable storage interface. Keep save/load migrations in mind; do not implement cloud sync yet.
- Support touch and mouse input; portrait-first mobile layout with usable macOS window behavior. Include accessibility labels, sound toggles, reduced-motion consideration, and adaptable screen sizes.

## 4. Game experience and screen specifications

### 4.1 Level map and options

- Main screen shows a path/graph of level nodes; finished levels are green, and show 1–3 stars based on best score. Locked levels are visually distinguishable. Display current coins and progression.
- Settings: music and sound effects on/off; selectable basic themes. Premium themes may appear only as clearly marked future placeholders or be hidden until monetization ships.
- The first release contains about **10 handcrafted levels** with increasing challenge; the design must permit additional levels without changing gameplay source code.

### 4.2 Launch-and-play loop

- Before launch, player can move the ball's starting point **only along the bottom launch line** and set launch angle/direction. An accessible Launch button starts the simulation. After launch, the default ball trajectory runs without further input; special controllable equipment is deferred until P2.
- A level defines one or more targets. Win requires contacting **all required targets** during an attempt; failure occurs when the ball is destroyed, exits allowed bounds, or exceeds the attempt time limit without completing the level.
- Display a visible aiming control, ball trajectory animation, hit effects, win/fail feedback, retry, and return-to-map affordances. Do not require network access.
- Level data defines launch bounds, ball initial properties, targets, obstacles, coin positions, limits, permitted equipment and objective rules. P0/P1 obstacles: simple bounce block and fixed-angle reflecting block. Defer black hole/gravity/anti-gravity/adjustable blocks to P2, but keep the engine extensible.

### 4.3 Coins, score, stars, and level progression

- Each coin collected adds **10 score points** to the current attempt; coin inventory awarded after a completed attempt follows explicitly documented rules to prevent unlimited retry farming. Default proposal for P1: collectable coins award currency only on the first successful completion of a level; a repeated win may improve best stars/score but cannot repeatedly mint the same coins.
- Base completion score and time bonus depend on level configuration. The exact formula and 1/2/3-star thresholds are configurable per level and should be chosen/tuned through tests, not hard-coded globally.
- A failed attempt applies a configurable **score penalty**, floored at zero. For P1, **never deduct purchased or earned coin balance on failure**. Clarify the future leveling rule separately: cumulative experience/points can unlock tiers, but subtracting a level threshold must not accidentally lose lifetime best scores.
- Winning unlocks the next nodes in the authored level graph. Record best stars and best score, not just the last attempt.

## 5. Expansion backlog (P2+, not for initial automatic execution)

- **Hazards:** black hole destroys on collision and curves nearby trajectories; gravity source attracts, anti-gravity source repels; fixed/adjustable reflectors bounce based on surface normal and incident velocity.
- **Ball types:** normal, light, heavy, light-jet, strong-jet, and manually toggleable jet variants (limited fuel and continuous thrust). Define mass, impulse, acceleration, and energy budgets in data.
- **Player equipment:** buy allowed obstacles/balls/cards with coins at a pre-level shop; inventory changes must be transactional and tested; a level may disallow particular items.
- **Cards:** a counter card can override one eligible restriction; card slots unlock based on progress **and** a coin cost; at most two permanent slots in a level; time-limited extra-slot effects are future inventory items. Do not silently define remaining card rules or an unfinished "if the number of users who use ..." rule; capture these as product decisions.
- **Mystery boxes:** rare seasonal awards; planned wood/stone/bronze/silver/gold/diamond tiers; reward probabilities, eligibility, anti-manipulation and platform policies must be decided before launch, especially for paid/randomized rewards.
- **Level designer:** standalone desktop GUI or Flutter tool for manual authoring, preview, validation, and parameterized generation. An automatic "solvability" check must output at least one replayable successful trajectory found by simulation; a finite search is **not** a mathematical proof of impossibility.
- **Multiplayer:** m-v-n with tools that alter other players' trajectories, including symmetric restrictions; detailed fairness, latency, matchmaking, authoritative rules and server scope need a separate design.

## 6. Quality requirements and Definition of Done

- **Unit tests:** collision response, forces, target detection, game termination, score/time/star calculations, coin accounting, unlocks, persistence and schema validation. Use seeded deterministic cases and numeric tolerances.
- **Regression tests:** fixed level fixtures with recorded successful inputs and expected outcomes; prevent physics or scoring changes from silently breaking existing levels.
- **Widget/visual tests:** level map states, settings, main game controls, win/fail overlays. Use golden tests where stable across CI rendering environments.
- **Integration tests:** launch → hit target → win → save progress → reopen; failed attempt and retry; earned-coin purchase flow once included. Use Flutter's `integration_test` package.
- **Performance:** target smooth 60 FPS on agreed reference devices; instrument frame timing and simulation throughput; record device/profile mode and baseline before enforcing numeric budgets. No performance claim without a measured test.
- **Continuous checks:** formatting, static analysis, focused tests, full unit/widget suite; platform builds where runners/toolchains exist. iOS/macOS builds require a supported Mac/Xcode runner, so Linux-only CI must not claim they passed.
- **For every executable ticket:** measurable acceptance criteria, targeted tests, passing configured verification, no credentials/secrets, a commit with Jira key, a draft PR with the Jira URL, and a PR link/comment on the Jira issue. Human approval and merge are mandatory; Codex never merges or marks tickets Done.
- **Definition of initial playable MVP:** at least 10 levels load from assets; full aim/launch/win/fail loop; basic reflectors, targets, coins, scoring/stars, unlock map and local saving work offline; automated checks pass; Android + macOS smoke-tested where available, iOS build/test validated on available Mac/Xcode tooling before calling it shipping-ready.

## 7. Automation, Git, and Jira rules

- Jira is the work-tracking source of truth. Epics contain Stories or Tasks, which may contain Subtasks. Use `Blocks` links for sequencing and explicit acceptance criteria per executable work item.
- The planner may create initial P0/P1 tickets automatically from this brief; **all new tickets must start unapproved**. Never add `codex-ready` automatically. A human reviews each issue and authorizes it by setting **status `Ready` AND label `codex-ready`**. The worker ignores Epics and unapproved items; parent Stories/Tasks with subtasks are grouping issues, not simultaneously executable tickets.
- The scheduler chooses eligible tasks by Jira priority (where configured), then creation date, after verifying blocking dependencies are Done. One task/worker initially; add parallelism only with explicit worktree and reservation controls.
- The agent uses an isolated Git worktree, respects `AGENTS.md`, runs tests, and cannot directly merge or deploy. The orchestrator commits with the Jira key, pushes a branch, creates a **draft PR**, posts it to the Jira issue, and moves the issue to **In Review**. Human reviewer approves/merges and then moves the issue to **Done**.
- Human approval gates are required for game-design ambiguities, platform account creation, new paid dependencies, infrastructure, online services, monetization, licenses and production release.
- Never commit `.env`, credentials, generated purchase keys, or other private configuration. Keep plan schema/plan YAML version-controlled if appropriate; back up the generated `plan.applied.json` Jira mapping separately.

## 8. Release readiness placeholders (do not publish yet)

Create `docs/release/README.md` containing future checklists and interfaces for Android Play Console, iOS App Store Connect/TestFlight, and macOS signing/notarization/distribution. Reserve space for package IDs, icons, app privacy disclosures, age-rating review, crash-reporting consent/SDKs, store screenshots, purchase validation, accessibility, app versioning, localization, and release signing. No actual store credentials, live IAP, ads, deployment, or uploads in P0/P1.

## 9. Open product decisions — request owner approval, do not improvise

1. Final game title, visual style, sound/art asset licenses, age rating and target audience.
2. Exact level-completion scoring/penalty formula, 1/2/3-star thresholds, coin reward/farming policy, and progression thresholds.
3. Whether all targets must be touched in one flight and whether an attempt can launch more than once (P1 default: one launch, all targets in the same attempt).
4. Economy design: price and persistence of equipment, cards, consumables, bonuses, gift probabilities and reward disclosures.
5. Detailed card effects, temporary-slot behavior, the incomplete multi-user card rule, multiplayer victory conditions and anti-cheat.
6. Subscription terms, ads frequency, paid themes, in-app purchase IDs, restore/refund policy, backend needs and app-store release mechanics.

When an ambiguity does not prevent the offline vertical slice, document it and proceed with explicitly stated P1 defaults. Otherwise make a blocked product-decision ticket, not an invented product rule.
