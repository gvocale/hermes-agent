# Canonical skill discovery fix

## Problem, evidence, ownership
- Live `skill_view('infra-fix-workflow')` reproduced ambiguity between a public skill and hidden `.infra-fix-workflow.versions/.../SKILL.md`.
- Shared `agent.skill_utils.iter_skill_index_files` follows directory links but prunes only enumerated hidden names; resolver direct and legacy-flat paths bypass that pruning. Canonical dedup exists in resolution but not shared traversal.
- Assigned existing clean worktree: `/Users/giovanni/Personal/hermes-agent-isolated-workflow`, branch `feat/isolated-workflow-policy`, base `90aaf9294b8835f8244679fe528f23a47b2ba06d`; verified origin `git@github.com:gvocale/hermes-agent.git`. This explicit assignment overrides creating another worktree. Product owner is upstream Hermes; publication is out of scope.
- Non-goals: isolation runtime, installed skill changes, private infra changes, publication, deployment, restart.

## Acceptance
- Hidden components below a discovery root never contribute skills, categories, direct-name/path or legacy-flat resolution candidates.
- Public symlinks into hidden version storage remain discoverable/loadable. Alias paths to one canonical target produce one index entry and no false collision; genuinely distinct targets retain collision refusal.
- Hidden ancestors of the configured root (e.g. `.hermes/skills`) do not hide that root.
- Preserve external directory/skill symlinks (intentional existing feature), support-file containment, project trust/quarantine, org gates, disabled/platform gates.
- Exercise real temporary HERMES_HOME and skill files via shared discovery, prompt index, skills_list and skill_view/registry dispatch, with failing regressions before source changes.

## Architecture decision
- `best-architecture` is not in the available skill catalog; assess explicitly here.
- Extend the existing shared walker and exclusion predicate, applying hidden rules to lexical root-relative paths, never resolved storage names. Canonical resolved identity is for dedup/cycle avoidance only.
- Reuse the shared predicate for resolver bypasses; preserve lookup aliases even when traversal emits only one path per target.
- Reject enumerating installer-specific `.versions/.pointer/.stage` spellings (unbounded suffixes), rejecting all symlinks (breaks supported checkout links), and filtering resolved absolute paths (hides public versioned installs and `.hermes` roots).
- Failure modes: cycles/dangling links, aliases with differing directory names, root-relative handling, hidden flat Markdown, duplicate frontmatter names on distinct files. Tests use real trees, not mocked filesystem traversal.
- No config/schema/prompt-cache invalidation changes; future sessions build the corrected index normally.

## Steps and review
1. Add focused end-to-end hidden-storage regression; run `scripts/run_tests.sh` and record RED.
2. Apply minimal shared filtering and resolver correction; verify GREEN.
3. Add canonical alias/cycle regression, observe RED, implement deterministic canonical dedup without losing explicit alias resolution; verify GREEN.
4. Run relevant skill discovery/resolution/security suites with `scripts/run_tests.sh`, inspect complete diff and compat/lint checks where available.
5. Hand uncommitted scoped diff to parent for independent review. No publish/restart.

## Activation, rollback, stop gates
- Component: Hermes discovery code; host: assigned local worktree only; profiles: temporary test profiles only; activation/installer: not applicable.
- All deployment hosts are not applicable by task scope. Installed state remains untouched.
- Rollback: discard only this reviewed patch if rejected; no live data migration or operational rollback required.
- Stop on unexplained regression, security-contract conflict, or need to edit outside assigned worktree. Preserve worktree for review.

## Evidence and handoff
- RED 1: `scripts/run_tests.sh tests/tools/test_skills_canonical_discovery.py --file-retries 0` failed all four hidden-storage cases because the shared walker emitted hidden SKILL.md copies. GREEN after lexical pruning and direct/legacy resolver filtering: four passed.
- RED 2: same command with `-k public_aliases` failed both internal/external target cases because three public aliases emitted three paths. GREEN after canonical file dedup and ancestor-cycle protection: six passed.
- RED 3: extending the hidden-storage invariant to the persisted prompt snapshot failed four cases: its separate manifest walker still indexed hidden copies. Replaced that duplicate traversal with the shared iterator; six passed. Existing snapshots containing hidden copies/aliases now invalidate naturally through manifest comparison; no format-version bump or mid-session cache mutation is needed.
- Final broader regression command: Python selects `test_*.py` whose filename contains `skill` or `prompt_builder` in `tests/{tools,agent,hermes_cli,gateway,cli,scripts}`, then invokes `scripts/run_tests.sh <selected paths> --file-retries 0 -j 6`. Result: **69 files, 987 passed, 0 failed, 2 skipped**, 47.3 seconds. No retries/flaky passes. This is the relevant subsystem suite, not the entire repository suite; Windows-specific coverage remains CI-only.
- Static checks: `ruff check agent/skill_utils.py agent/prompt_builder.py tools/skills_tool.py tests/tools/test_skills_canonical_discovery.py`, `python3 scripts/check_compat_pointers.py`, and `git diff --check` passed. Added source lines scanned for credential assignments, dangerous eval/deserialization and shell injection; no matches.
- Coverage (behavioral, not a measured line percentage): hidden versions/pointer/stage-rollback/nested directories; root-relative handling beneath `.hermes`; hidden SKILL.md/DESCRIPTION.md/legacy flat Markdown; registry list/view; bare and categorized alias lookups; directory and file symlink canonical identity; internal and external link targets; cycles/dangling links; true collision refusal; support-reference reads and symlink escape rejection; prompt manifest and pre-fix snapshot replay. Existing tests additionally exercise project trust/quarantine, org namespace gates, disabled/platform rules, profile scope, caches, slash commands, sync and managers.
- Final files: `agent/skill_utils.py`, `agent/prompt_builder.py`, `tools/skills_tool.py`, new `tests/tools/test_skills_canonical_discovery.py`, and this plan.
- `search_files` returned Operation not permitted; bounded Python filesystem discovery through terminal was used instead. No test/environment blocker remains.
- Independent review is assigned to the parent after this handoff; no claim of independent approval, CI, publication or deployment. Changes remain uncommitted in the assigned worktree.

## Reusable lesson
Visibility follows the lexical path below the configured root; identity follows the resolved file. Do not apply hidden-name filters to resolved targets or root ancestors. Use the same traversal in prompt snapshot manifests as live discovery, and preserve alternate lookup names until after candidate matching. Recorded here rather than an installed skill because installed-file edits are explicitly out of scope.
