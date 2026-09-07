# Astra footer effort regression: fix plan

## Problem and root cause
- Symptom, impact and evidence/reproduction: a Slack reply rendered `gpt-6-astra · 54% · 37s` even though the configured footer field list includes `reasoning`. The deployed checkout's `gateway/runtime_footer.py` recognizes only model, context, latency, and cwd, while commit `74a7607f2a` added reasoning and other requested metrics and commit `f296cb471f` added Slack model emoji. Neither commit is an ancestor of the current upstream-based deployed main.
- Root cause: Giovanni-specific product commits were published on divergent `gvocale/hermes-agent` main but subsequent host updates selected `NousResearch/hermes-agent` main directly. That deployment-source split silently dropped the footer extensions. Thread age is not causal; each new footer is formatted by the currently loaded gateway code.
- Owner repo, verified origin, base SHA, branch/worktree: `gvocale/hermes-agent`; base `f296cb471f984d9a3f9d7f1db118890a69c431a4` (`fork/main`); branch `fix/astra-footer-effort-regression`; worktree `/Users/giovanni/.hermes/worktrees/astra-footer-effort-regression`.
- Scope and non-goals: reconcile current upstream main into Giovanni's main while retaining the fixed-avatar/model-emoji footer and all runtime metrics, add a regression contract for Astra effort, then deploy the resulting published SHA. No changes to provider reasoning semantics or Slack thread history.

## Acceptance criteria
- Observable behavior before/after: with resolved model `gpt-6-astra`, Slack platform, configured effort `low`, and fields including reasoning, footer contains `:openai: gpt-6-astra · low`; existing metrics remain available. Non-Slack output remains emoji-free.
- Regression and integration checks: prove the Astra test red against current `origin/main`; merge current `origin/main` into the branch; run `scripts/run_tests.sh` for runtime footer, gateway caller, model command, Slack adapter/streaming contracts; run diff check and required lint/type checks selected by the repo.
- Per-host applicability: Hermes product checkout and gateway on Personal MacBook Pro, Mac mini, and Valon. Restart only gateway processes after the same published SHA is installed.

## Architecture decision
- best-architecture availability and guidance used: unavailable. Assessment performed here.
- Alternatives considered: (1) attribute the omission to an old thread, rejected because footer formatting happens per turn; (2) add a one-off Astra special case, rejected because the formatter already has a generic reasoning field; (3) keep deploying upstream and reapply local patches after every update, rejected because it recreates the same regression; (4) reconcile upstream into Giovanni's product main and deploy only that published source, selected.
- Chosen design and ownership: retain generic reasoning propagation from the effective per-turn reasoning config, retain Slack-only model emoji, merge current upstream into `gvocale/hermes-agent` main, and make that published main the three-host source of truth.
- Compatibility/security/failure modes: no secrets or persisted session data change. Unknown/missing reasoning remains omitted. Old threads gain the field on their next turn once the gateway is restarted. Main drift, merge conflict uncertainty, test failure, unreachable host, or failed restart stops rollout.

## Implementation and review
- Ordered edits: add Astra regression test first; run it red on upstream behavior; reconcile upstream; resolve only relevant conflicts; run green tests; independently review the complete delta.
- Required CI: repository-focused tests plus GitHub checks before merge.
- Reviewer: independent subagent; blocking findings must be resolved before publication.

## Deployment
- Component / host / activation: Hermes product checkout and messaging gateway on Personal MacBook Pro (canary), Mac mini, then Valon last. Use the repository/private-infra-owned deployment path and remote controller topology.
- Preflight: clean checkouts, verified origins, exact published main SHA, reachable controllers, prior SHA recorded, and no active-work restart boundary violations.
- Stop conditions: failed check, unreachable host, main drift, dirty checkout, or activation failure.

## Rollback
- Previous known-good host SHA: record immediately before deployment in private evidence.
- Supported rollback: revert the merged fix on `gvocale/hermes-agent` and publish the revert as a new main tip, then redeploy serially.
- No data migration or backup is required. Trigger rollback on gateway startup failure, Slack reconnect failure, or footer/streaming regression.

## Per-host evidence
- Record checkout/remote/installed SHA, previous and replacement gateway PID/start boundary, Slack connection state, and live footer acceptance result for each applicable host outside Git when it includes runtime identifiers.

## Closeout
- Verify PR merged and exact `gvocale/hermes-agent` main SHA.
- Verify all three hosts converge and the Astra footer includes effort on a fresh post in the existing thread or an equivalent live Slack turn.
- Remove the worktree without force only after merge and rollout verification; delete the branch after preservation is confirmed.
