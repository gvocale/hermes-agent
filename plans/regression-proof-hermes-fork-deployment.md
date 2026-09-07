# Regression-proof Hermes fork and deployment architecture

> **For Hermes:** Implement the infrastructure tasks below through `infra-fix-workflow`, one vertical slice at a time with TDD and independent review.

**Goal:** Make it impossible for an upstream update or host-local command to silently remove Giovanni-required Hermes behavior.

**Architecture:** `gvocale/hermes-agent/main` becomes the only deployable Hermes product authority. `NousResearch/hermes-agent` remains a read-only `upstream` input, never a deployment source. The private `gvocale/hermes` deployer accepts only an exact current fork-main SHA, validates required behavior contracts before mutation, rolls out serially, and verifies the executing revision plus behavior on each host.

**Why this is needed:** The repeated regressions are not isolated formatter bugs. The three hosts were updated from `NousResearch/hermes-agent/main` while required behavior lived only on divergent `gvocale/hermes-agent/main`. Git considered both histories valid, and deployment checks verified SHA consistency without verifying the correct repository or required behavior. The process therefore allowed a clean, successful deployment that was functionally wrong.

---

## Decision

Adopt **one canonical fork, one deployment path**:

1. Product source of truth: `github.com/gvocale/hermes-agent`, branch `main`.
2. Every host checkout:
   - `origin` = `gvocale/hermes-agent`
   - `upstream` = `NousResearch/hermes-agent`
   - local `main` tracks `origin/main`
3. Upstream changes enter through a reviewed integration branch in the fork. They never deploy directly.
4. Production updates happen only through `gvocale/hermes`'s exact-SHA deploy command.
5. `hermes update` on managed hosts must refuse or redirect to that deployer. There is no override that can deploy upstream directly.
6. Deployment success requires both provenance and behavior, not merely a clean checkout or matching SHA.

This is intentionally subtractive. It removes the second deployment authority instead of trying to make two authorities agree.

## Rejected alternatives

### Keep current remotes and add warnings
Rejected because warnings preserve the unsafe operation. A valid-looking `origin/main` update can still erase fork behavior.

### Apply a patch stack after every upstream update
Rejected because it creates a second composition mechanism. A patch can apply cleanly while becoming disconnected from the runtime path.

### Move everything into a plugin immediately
Deferred. It is attractive only after Hermes exposes stable extension points for footer data propagation and Slack final-message formatting. Today it would add compatibility machinery and can still fail open if the plugin stops loading.

### Vendor Hermes inside `gvocale/hermes`
Rejected because it blurs product and infrastructure ownership. Product code stays in `gvocale/hermes-agent`; deployment policy stays in `gvocale/hermes`.

---

## Non-negotiable invariants

- No commit from `NousResearch/hermes-agent` is directly deployable.
- Remote names alone are insufficient. Normalized fetch and push URLs must identify the expected owners.
- Managed-host `main` tracks `origin/main`, where `origin` is `gvocale/hermes-agent`.
- Deployment accepts a full immutable SHA and requires it to equal GitHub's current `gvocale/hermes-agent/main` tip before any host mutation.
- The candidate must descend from the last deployed fork SHA. Rollback is a new revert commit on fork main, never a detached history rewind.
- Required GitHub checks must be successful for the exact SHA in the fork repository.
- All three Macs receive one SHA, serially: Personal MacBook Pro, Mac mini, Valon last. Stop on first failure.
- Checkout SHA, installed SHA, and executing gateway SHA must all match.
- Behavior probes must prove the Slack footer contract, including model identity, effective reasoning effort, context, latency, throughput, cache hit, and total tokens.
- Missing or stale provenance, missing CI, wrong remote, wrong tracking branch, dirty source, failed behavior probe, or unavailable GitHub all fail closed before mutation.

---

## Task 1: Recover and establish the canonical fork tip

**Repository:** `gvocale/hermes-agent`

**Objective:** Reconcile the latest `NousResearch/main` into the fork without losing any fork-only behavior.

**Steps:**
1. Start from current `gvocale/main` in a clean worktree.
2. Fetch the exact current `NousResearch/main` SHA.
3. Merge it into an integration branch without auto-committing.
4. Run the required downstream contracts before committing.
5. Independently review conflict resolutions and the downstream behavior delta.
6. Push a PR to `gvocale/main`, require green checks, merge, and read back the exact fork-main SHA.

**Required product tests:**
- `tests/gateway/test_runtime_footer.py`
- `tests/gateway/test_model_command_context_offload.py`
- `tests/gateway/test_slack.py`
- `tests/gateway/test_slack_native_streaming.py`
- `tests/gateway/test_stream_final_contract.py`
- `tests/gateway/test_slack_model_picker.py`
- An integration-level gateway-turn test proving resolved Astra effort reaches the Slack footer.
- Explicit transport assertions that neither `chat.postMessage` nor native stream start sends `icon_url` or `icon_emoji`.

**Acceptance:** Slack Astra fixture renders `:openai: gpt-6-astra · low · 54% · 37s`, non-Slack has no Slack emoji, and all existing downstream contracts pass on the merged tree.

## Task 2: Add Hermes product authority to the private infra manifest

**Repository:** `/Users/giovanni/Personal/hermes-infra` (`gvocale/hermes`)

**Likely files:**
- `infra/components.toml`
- `infra/hosts.toml`
- `infra/lib/deploy.py`
- `tests/infra/test_manifest_deploy.py`
- New focused tests under `tests/infra/`

**Objective:** Represent Hermes product deployment as a first-class manifest component owned by `gvocale/hermes-agent`.

**TDD slices:**
1. Test that a component may declare product repository, checkout path, required GitHub checks, required behavior probe, and `restart-hermes-gateway` activation.
2. Add the minimal manifest/schema support.
3. Test the serial host order and controller topology.
4. Add the product component for all three Macs.

**Acceptance:** `infra-status` reports product checkout, fork-main SHA, executing SHA, and behavior-contract state per host.

## Task 3: Enforce repository authority before mutation

**Repository:** `gvocale/hermes`

**Objective:** Reject every source topology that could recreate this regression.

**TDD negative tests:**
1. `origin` points to NousResearch: fail before mutation.
2. `origin` fetch is correct but push is wrong: fail.
3. local `main` tracks `upstream/main`: fail.
4. requested SHA exists upstream but not at fork main tip: fail.
5. fork main advanced after the requested SHA: fail.
6. deploy checkout is dirty: fail.
7. required GitHub check is missing, pending, cancelled, or failed: fail.
8. candidate is not descended from last deployed fork SHA: fail unless it is a published revert tip.

**Implementation:** Add one product preflight in `infra/lib/deploy.py`; do not scatter checks across shell fragments.

**Acceptance:** Every negative fixture leaves files, installed artifacts, and gateway processes untouched.

## Task 4: Migrate all host remotes and tracking branches

**Repository:** `gvocale/hermes` for the migration command and tests.

**Objective:** Make ordinary Git semantics point to the correct authority.

**Target topology on each Mac:**
```text
origin   git@github.com:gvocale/hermes-agent.git
upstream git@github.com:NousResearch/hermes-agent.git
main     tracks origin/main
```

**Safety:**
- Inventory and preserve unique work before changing anything.
- Refuse dirty or divergent checkouts until unique commits are published through a fork PR.
- Do not reset, clean, stash, or copy source host-to-host.

**Verification:** Normalize and compare both fetch and push URLs, tracking ref, fork tip, local SHA, and absence of local changes.

## Task 5: Remove unmanaged update authority

**Repositories:** `gvocale/hermes-agent` for managed-host updater behavior, `gvocale/hermes` for policy/configuration.

**Objective:** Ensure `hermes update`, dashboard update controls, cron, or ad hoc scripts cannot switch managed hosts to upstream.

**Design:** Add one managed-install marker written by the infra installer. When present, all built-in source-update entry points refuse with one command directing the operator to the infra deploy workflow. Do not add a bypass flag.

**Tests:**
- Every updater entry point refuses before Git refs, files, dependencies, or processes change.
- An unmanaged upstream installation retains normal Hermes update behavior.
- The refusal names the infra-owned command and current installed SHA.

## Task 6: Gate deployment on behavior contracts

**Repository:** `gvocale/hermes`

**Objective:** Detect semantic loss even when merges, SHAs, and files look valid.

**Pre-deployment probe:** Execute deterministic tests from the candidate checkout for:
- Effective Astra reasoning effort propagation.
- Slack model emoji mapping and unknown fallback.
- Runtime footer metrics and omission rules.
- Fixed app-avatar behavior, specifically no per-message identity overrides.
- Streaming final-message contract.

**Post-activation probe:** Invoke the installed formatter and real gateway caller path locally with fixtures. Do not send production Slack messages. Require the executing code's output to match the contract version.

**Acceptance:** Removing any required field or disconnecting caller propagation blocks deployment before the next host.

## Task 7: Attest installed and executing provenance

**Repositories:** `gvocale/hermes-agent`, `gvocale/hermes`

**Objective:** Detect the case where the checkout is new but the running gateway is old.

**Artifact/runtime fields:**
- repository: `gvocale/hermes-agent`
- full source SHA
- artifact digest
- behavior-contract schema version

**Tests:**
- Substituting an older executable under a correct checkout fails verification.
- Tampering with artifact or provenance fails installation/startup.
- Gateway restart must produce a changed process/start boundary and report the expected SHA.

## Task 8: Deploy serially and prove convergence

**Order:**
1. Personal MacBook Pro, canary.
2. Mac mini.
3. Valon, last.

For each host, require:
- Correct remotes and branch tracking.
- Clean checkout at exact fork-main SHA.
- Installed and executing SHA/digest match.
- Gateway PID/start boundary changes where activated.
- Gateway health and Slack reconnection.
- Local behavior probe passes.

Stop immediately on failure. Report partial convergence honestly. Roll back only by publishing a revert to fork main and deploying that new tip through the same gates.

## Task 9: Add continuous drift detection

**Repository:** `gvocale/hermes`

**Objective:** Detect future drift before Giovanni notices missing UI behavior.

**Audit checks:**
- Remote owner/URLs and branch tracking.
- Local, fork-main, installed, and executing SHAs.
- Artifact digest and behavior-contract version.
- Managed-updater disabled state.
- Footer/identity behavior probe.

**Operation:** Run on a schedule from the existing infrastructure controller and alert only on drift or missing audit runs. A missing scheduled run is failure, not silence.

## Task 10: Document upstream intake

**Repositories:** `gvocale/hermes`, optionally `gvocale/hermes-agent` contributor docs.

**Workflow:**
1. Fetch `upstream/main`.
2. Create an integration branch from `gvocale/main`.
3. Merge one named upstream SHA.
4. Run full required downstream contracts.
5. Review conflicts and downstream behavior delta.
6. Merge only into `gvocale/main`.
7. Deploy the resulting exact fork-main SHA through private infra.

No host may pull upstream directly.

---

## Rollout sequence for this architecture

1. Complete the current Astra recovery PR and deploy it so service is restored.
2. Implement Tasks 2–4 in `gvocale/hermes`; deploy remote topology migration.
3. Implement Task 5 to remove unmanaged updater authority.
4. Implement Tasks 6–7 for semantic and runtime attestation.
5. Add scheduled audit and documentation.

Each stage must preserve a usable rollback path and must leave all hosts converged before proceeding.

## Success criteria

- A direct upstream-only SHA cannot pass deployment preflight.
- `hermes update` cannot mutate a managed host.
- A merge that removes reasoning, metrics, emoji mapping, or fixed-avatar semantics fails CI.
- A host running old code beneath a current checkout fails verification.
- All three Macs report one exact `gvocale/hermes-agent/main` SHA and passing behavior-contract version.
- Upstream adoption remains possible through one reviewed, tested fork-integration workflow.
