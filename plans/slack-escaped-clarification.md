# Escaped Slack clarification recipient: investigation and architecture plan

## Status and scope

Implementation and independent review are complete. The user now explicitly authorizes commit, push, PR and merge to `gvocale/hermes-agent` only, subject to checks and protections. Deployment is not authorized in the publication task. No live checkout, profile/config/skill, service or Slack mutation. Publication preflight refetched origin/main: it still equals base 90aaf9294b8835f8244679fe528f23a47b2ba06d; no drift or overlap. Historical investigation/implementation receipts below retain their original scope and results; this status and the publication gate supersede their earlier uncommitted-only instructions.

Implementation choice: generic authenticated `clarify_recipient` metadata (platform, workspace, user, originating chat), bound by the real callback only when its status destination is the originating adapter/chat. Slack validates workspace against its routed client. Routed/home-channel destinations omit the recipient rather than infer tenant binding. No compatibility stripping: even a leading model token remains literal prose, while the separate heading supplies the notification. One mention per prompt, never per option/update. Keep empty-choice fallback unchanged. Bound escaped presentation without splitting entities.

- Worktree: `/Users/giovanni/Personal/worktrees/slack-escaped-clarification-20260908`
- Branch: `debug/slack-escaped-clarification-20260908`
- Base: fetched `origin/main`, `90aaf9294b8835f8244679fe528f23a47b2ba06d`.
- Verified origin: `git@github.com:gvocale/hermes-agent.git`.
- Product ownership: Hermes Slack adapter / gateway. The infrastructure ownership matrix ordinarily routes product changes to `NousResearch/hermes-agent`; this task explicitly authorizes isolated local implementation and verification in the fork. **No upstream PR without a new explicit user request.**
- Read-only installed comparison: `/Users/giovanni/.hermes/hermes-agent` HEAD matches this base; `git diff --no-index` of the installed and worktree Slack adapter returned no differences, exit 0. This establishes on-disk parity, not the code loaded by a running gateway.
- `best-architecture` is absent from the supplied skill catalog; it was not invoked. Alternatives, ownership, compatibility, security, failure modes and testability are assessed below using the loaded infra-fix-workflow, systematic-debugging, Hermes, and Slack integration guidance and repository rules.

## Problem and evidence boundaries

The parent investigation supplied an exact permalink-reader result for an answered clarification: a check-mark decision followed by a question containing an entity-escaped user mention. The parent also verified the ID belongs to the requesting human, not the bot, and reported the same pattern in two other answered clarifications. Private thread text, IDs, permalink and application-specific decision details are deliberately not copied into Git.

Minimal synthetic symptom:

```text
Input question: <@U123ABCDE45> Apply the correction?
Outgoing question: ❓ &lt;@U123ABCDE45&gt; Apply the correction?
Answered reader form: ✅ synthetic-user: Apply correction
                      ❓ &lt;@U123ABCDE45&gt; Apply the correction?
```

Impact: the intended recipient token is literal text rather than active Slack mention syntax in the multi-choice question. This investigation verifies payload bytes, not desktop rendering or receipt of a notification. The original model tool arguments and raw Slack interaction payload were not fetched, so exact historical input encoding and Slack-side storage transformations remain unobserved. Those limitations do not prevent deterministic reproduction of the defect from an unescaped input on the current base.

## Verified root cause and data flow

The Block Kit multi-choice renderer treats the entire question as literal explanatory text. It has no separate trusted recipient lane. A legitimate recipient embedded in the question is escaped along with arbitrary markup. The answer update preserves that already-rendered question; it does not introduce the escaping in the reproduced path.

Line references below describe the recorded diagnosis base (before implementation):

1. `tools/clarify_tool.py:123,174,215-225` normalizes/strips question text and invokes the platform callback; it does not HTML-escape the question.
2. `gateway/run_turn_runner.py:1170-1207` registers the original question and passes the same `question` to `send_clarify`, along with status-thread metadata. No mention transformation happens here.
3. **`plugins/platforms/slack/adapter.py:5184-5187`** replaces `&`, `<`, and `>` unconditionally. This is the first demonstrated lossy boundary for active mention semantics.
4. `adapter.py:5195-5196,5224-5231` puts the escaped question in both fallback and section and invokes `_send_interactive_prompt(..., sanitize=False)`. Long choice explanatory text is separately escaped at `5199,5205`.
5. `adapter.py:4686-4700` runs the real builder, then `4673-4684` posts the resulting text/blocks directly. No general `format_message` pass rescues mentions.
6. `adapter.py:5433-5446` acknowledges/authorizes a click and obtains the first section via `_section_text(..., limit=None)` (`5311-5319`), unchanged.
7. Choice, Other and expired branches call `_update_clarify_message` (`5451-5457,5474-5484`). This forwards to `_finalize_interactive_message` with `sanitize=False` (`5426-5430`). `5325-5331` sends the same question as the first section and only the decision as top-level fallback. No additional HTML escape occurs in these adapter updates.
8. The permalink reader uses `_render_message_text` (`5528-5536`): it joins decision fallback and unseen question section (`5637-5662,5683-5686`). The synthetic answered readback reproduces the check-mark-plus-escaped-question form without the reader introducing entities.
9. Working contrast: ordinary `format_message` protects Slack entities before escaping (`2907-2977`, particularly `2959`); the same raw user token survives this formatter. `send_clarify` without choices delegates to the base (`5174-5177`), so multi-choice cards take a distinct rendering path.

### Hypotheses and results

| Ranked hypothesis | Falsifiable prediction | Result |
|---|---|---|
| Multi-choice question renderer escapes the token | Raw valid mention already escaped in first `chat_postMessage` | Confirmed in both fallback and section |
| Answered-message update double-escapes it | Question gains an encoding layer between send and `chat_update` | Falsified in adapter replay for choice, Other and expired: section is byte-identical |
| Input was already entity-escaped | Pre-escaped input becomes `&amp;lt;...&amp;gt;` on initial send | Confirmed as a possible additional layer, but unnecessary to reproduce and not established for historical tool input |
| Reader creates the appearance | Clean outgoing block becomes escaped only when read | Falsified in synthetic actual reader helper: input block is already escaped and the reader preserves it |

### Intent and history

- `95aad9229b066a7f40285bf946c770a423a2be6a` introduced Slack clarify buttons. Its original comment explicitly says to escape controls so questions render literally “instead of as markup/mentions.” This was deliberate literal rendering / mention neutralization, not a recent accidental encoder insertion.
- `45c1a9264124859aaff4ce46e37e87f500ccfeea` shortened decision buttons and extracted the same replacement chain into `_escape`; its diff retained question escaping and extended it to explanatory options. It did **not** introduce this mention behavior.
- Existing `tests/gateway/test_slack_clarify_buttons.py:205-221` requires literal `<A> & <B>` escaping but had no trusted-recipient coverage. All 8 pre-existing tests passed before adding diagnostics.

Do not remove the safety behavior globally. The architectural gap is mixing notification intent and untrusted question prose in one string.

## Tight feedback loop and executed verification

All tests ran through `scripts/run_tests.sh`, which selects the shared venv, strips credentials and supplies isolated test homes/subprocesses. Real production adapter methods, action handler, clarify registry and reader helper were imported; the Slack SDK/network transport was replaced by the existing fake-client harness. No connection or API request was made.

Added **two diagnostic test functions**, with five parameterized cases, to `tests/gateway/test_slack_clarify_buttons.py` (class starts at line 312):

- Three lifecycle cases characterize where escaping happens, preserve canonical choice resolution, compare the ordinary formatter, and probe pre-encoded input.
- Two strict expected-failure cases assert active intended-recipient syntax in initial fallback and section. They are symptom probes, **not a final policy contract authorizing arbitrary prose mentions**. When implementing the selected design, supply trusted recipient metadata in those cases and add negative security cases before removing xfail.

Executed commands/results:

```sh
scripts/run_tests.sh tests/gateway/test_slack_clarify_buttons.py
# Before edits: 8 passed, 0 failed; 1.1s runner wall.

scripts/run_tests.sh tests/gateway/test_slack_clarify_buttons.py \
  -k test_intended_recipient --runxfail --file-retries 0
# Exit 1: 2 failed, 11 deselected; 0.8s runner wall.
# Both failures: expected '<@U123ABCDE45>' in
# '❓ &lt;@U123ABCDE45&gt; Apply the correction?'

scripts/run_tests.sh tests/gateway/test_slack_clarify_buttons.py \
  tests/gateway/test_slack.py tests/tools/test_clarify_tool.py \
  tests/tools/test_clarify_gateway.py \
  tests/gateway/test_clarify_send_timeout_ambiguity.py --file-retries 0
# Final revision: 332 passed, 0 failed, 2 xfailed across 5 files; 8.8s runner wall.
# Per file: clarify buttons 11 passed + 2 xfailed; Slack 235;
# clarify tool 48; clarify gateway 27; timeout ambiguity 11.

ruff check tests/gateway/test_slack_clarify_buttons.py
# All checks passed.
/Users/giovanni/.hermes/hermes-agent/venv/bin/python -m py_compile \
  tests/gateway/test_slack_clarify_buttons.py
# Exit 0.
git diff --check
# Exit 0.
```

The shared Python environment does not have the `ruff` module (`python -m ruff` failed); the existing standalone `ruff` executable succeeded. No dependency installation was needed. One broad file search transiently returned Operation not permitted; narrower searches succeeded. The full repository suite, CI, live Slack notification delivery and fleet runtime checks were not run. Passing surrounding tests plus expected failures is **not** a fixed bug or green acceptance.

## Selected minimal architecture correction

Keep notification identity separate from question content, using existing per-turn metadata rather than a new tool parameter, core tool, global setting, or plugin system.

1. The gateway callback owns trusted context: originating user, workspace, destination and thread. Pass a dedicated clarify-recipient identity through a copied metadata dictionary, derived only from that turn's authenticated source. Do not let model question text choose an ID or workspace. For externally routed/home-channel prompts, require a verified destination-workspace binding; omit notification rather than borrowing a foreign user ID.
2. The Slack adapter owns Slack mention syntax. Require an exact valid Slack user ID and matching workspace from trusted context. Missing/malformed/ambiguous identity means no active mention. Human-requested clarification is the concrete consumer, not a generic notification framework.
3. Build one small Slack-specific clarification presentation with a trusted heading recipient and literal question/options. Generate the section and fallback from it. Continue escaping arbitrary question prose, code, options, links and broadcast/subteam/user tokens. Do not run the whole question through `format_message`, and never globally HTML-unescape it.
4. To avoid displaying the old escaped recipient redundantly, an optional compatibility normalization may remove only an exact leading token matching the trusted recipient, raw or the explicitly recognized single-escaped form, outside quotes/code. It must not decode arbitrary entities or interpret labeled, nested, malformed, middle-of-sentence or other-user tokens. A separate heading works even when the model emits no mention at all. Decide exact normalization cases with tests before implementation.
5. Preserve the canonical choice strings and index action values unchanged; only render-time explanatory text is neutralized. Keep outcome rendering literal too, since canonical choice text and display names are currently interpolated directly into mrkdwn (`5476`, `5330`). This is a related trust-boundary acceptance requirement, not evidence it caused this incident.
6. Keep rendered question sections stable through answered/Other/expired updates; do not escape or unescape the section a second time. Do not add a new notification to answered/expired fallback. Unknown legacy cards must remain safe and must not be mass-edited to activate old mentions.
7. Reuse a topical Slack renderer module (for example the existing `block_kit.py` boundary) for pure presentation logic rather than growing the already large adapter with another inline parser. No broad adapter refactor in this task. If repository size rules require structural extraction, isolate it as a prerequisite to a later implementation, with behavior-preserving tests.

Metadata pitfall: `gateway/run_turn.py:2806-2813` adds `recipient_user_id` to native-task-card **progress** metadata, while `2822-2827` constructs **status** metadata separately, which the clarify callback sends. Do not assume streaming recipient metadata automatically reaches clarification. Verify the actual callback-to-adapter path for native streaming on/off, no-thread and thread cases. The design should derive its own turn-bound context, not read a process-global “current user.”

### Alternatives assessed

| Alternative | Decision |
|---|---|
| Remove `_escape` or globally unescape `<@...>` | Reject: resurrects arbitrary user, subteam and broadcast mentions; interprets literal examples as notifications |
| Use ordinary `format_message` for all questions | Reject: changes literal content/markdown contract and preserves a broader entity set than the one trusted recipient |
| Preserve every valid user token with a regex | Reject: syntactic validity is not authorization or notification intent |
| Preserve only first question token without metadata | Reject: position is not trust; a model or quoted content can target another human |
| Decode only during answered update | Reject: too late for the original notification and creates new mentions in historical content |
| Fix only the reader | Reject: hides evidence without changing the outgoing Slack payload |
| Add a new model-facing recipient parameter/config system | Reject: unnecessary core surface and another untrusted identity source |
| Separate trusted heading via existing metadata | Select: smallest adequate boundary, deterministic and testable, preserves literal prose semantics |

Compatibility: additive internal metadata, no tool schema change, no prompt-cache changes, no data migration, canonical actions unchanged. Non-Slack adapters should see unchanged behavior. Batch prompts must receive the same correct turn identity without adding duplicate notifications. Decide notification frequency for a batch before rollout; do not silently introduce one ping per option or per update.

Failure modes: absent/foreign identity fails closed; oversize sections must reserve recipient/header budget without cutting a token/entity; client/update failure retains existing honest error/expiry behavior. Observability should log render mode and send/update success, not private questions, answer text or credentials. A source change without gateway activation is not a completed rollout.

## Future implementation acceptance and review gates

Implementation is authorized. These original acceptance gates guided the local fix; executed evidence and explicit limitations appear in the implementation receipt below.

1. Add real caller-path tests proving the authenticated source identity reaches a fake Slack transport through `_clarify_callback_sync`, with temporary Hermes home, streaming on/off, channel/thread and routed destinations. Verify ambiguous workspace cannot select a different tenant client.
2. Replace diagnostic policy probes with the trusted-context contract: intended heading mention appears raw once in both initial fallback and section; same content with absent/foreign/malformed recipient context has no active mention.
3. Security cases: another user, labeled mention, `<!channel>`, `<!here>`, `<!everyone>`, subteam, code/quoted mention, arbitrary `<A>`, ampersands, raw and single/double-encoded input; none becomes active through decoding. Test explanatory choices and answer display names while canonical returned choice remains byte-identical.
4. Lifecycle cases: answered, Other, expired, duplicate click, delayed send, reset/timeout; rendered section remains stable, no new update-time notification; no migration or arbitrary rewriting of legacy cards.
5. Compatibility cases: empty choices/open-ended, batch semantics, long options/short positional buttons, recommended suffix, exact Slack section/fallback limits, Unicode near limits, no-thread/DM and non-Slack fallback behavior.
6. Run the focused five-file command above, relevant new caller-path and Slack Block Kit/security tests, compile/lint, and required owner-repository CI. Remove expected failures only when the intended security contract is green. Review complete diff for scope, secrets and routing; obtain independent review before any publication.
7. With fresh user authorization, select an approved publication destination (no automatic upstream PR), then follow repository-owned rollout instructions. Stop if task scope or ownership remains ambiguous.

## Future rollout / activation / rollback matrix

**All deployment fields are pending, not measured.** Installed source comparison above is the only installed evidence collected; no host inventory, service health, credential, process or profile read was needed for this diagnosis. Installer command/component names, real host aliases, affected profiles and recovery receipts must be discovered from the owning repository manifest before execution, not guessed here.

| Component | Host applicability | Profile | Activation | Controller | Verifier / rollback |
|---|---|---|---|---|---|
| Slack adapter + trusted clarify caller metadata | Personal MacBook Pro, if running this adapter/version; first applicable canary | Inventory required | Publish selected SHA, repository-owned install; remote graceful affected-gateway restart | Mac mini | New loaded revision/process boundary, health and Slack reconnection; synthetic authorized clarify readback + notification check; restore recorded prior version via supported installer and verify recovery |
| Same | Mac mini, if applicable; after canary | Inventory required | Same published SHA; only affected gateways | Valon | Same acceptance and rollback checks |
| Same | Valon MacBook Pro Work, if applicable; last | Inventory required | Same published SHA; only affected gateways | Mac mini | Same; never transfer Work Slack credentials to personal hosts |
| Diagnostic tests and plan only (current task) | Local isolated worktree only | None | No runtime activation | None | Tests and Git scope check; preserve worktree for review |
| MoneyTree services, skills/config, unrelated adapters | Not applicable to this diagnosis | None | None | None | No restart, installation or repair |

Future preflight must record exact published SHA, clean checkout/origin, installed SHA/digest, process revision/start, affected profile and previous known-good version for each applicable host in private receipts. Confirm rollback controller and supported installer before replacing anything. If the chosen private infra installer forbids older SHAs, publish a revert as a new main tip rather than forcing an old revision. No rollback command is invented here.

Stop on failed tests/security review, main drift, unresolved destination-workspace binding, unreachable required host/controller, unknown installer/activation class, lost profile features, failed activation or failed notification acceptance. Roll out serially and stop at the first failure. Restore the recorded last-known-good implementation through the owning supported path, restart only affected gateways from the designated remote controller, and verify both health and clarify behavior. There is no persistent schema migration to reverse; already-posted messages are not mass-edited.

Live acceptance, only when explicitly authorized: send a minimal synthetic question mentioning the verified consenting recipient through the real clarify caller, confirm initial block/fallback mention syntax by exact readback, have the recipient confirm notification and tap a choice, read back the same message and verify resolution/section stability. A successful API call alone is insufficient. Store private Slack evidence outside Git.

## Implementation receipt — ready for independent review

### Decisions and discovered constraints

- The real `TurnRunner._clarify_callback_sync` creates fresh `clarify_recipient`
  metadata from `ctx.source` (platform/workspace/user/chat); it discards any inherited
  value and binds only the originating adapter and chat. No model schema, prompts,
  cached context, configuration, global user state or new tool were changed.
- `block_kit.py` owns the small pure heading/escape/budget functions. A validated
  U/W user ID (uppercase alphanumeric, 9–32 characters total) produces one active
  heading token per prompt in both section and fallback. Prose tokens remain literal,
  including matching leading tokens; compatibility stripping was deliberately omitted.
- **Legacy transport constraint discovered during implementation:** clarify passes
  `team_scoped_key=False`, which also makes `_post_interactive_blocks` select the
  client from the channel map rather than explicit workspace metadata. We did not
  broaden this fix into a transport/interaction-key migration. The heading therefore
  requires the exact known channel workspace, a registered client for that workspace,
  matching recipient workspace/chat/platform, and no conflicting explicit workspace.
  Unknown/ambiguous/foreign context gets no active heading; the existing plain card
  routing behavior is unchanged. This is fail-closed notification identity, not a
  claim that pre-existing ambiguous multi-tenant card routing is fixed.
- Direct D-channel prompts with a known channel/workspace map work. Bare U/W-target
  synthetic sends or unknown-channel primary-client fallbacks do not gain a mention.
  Routed/home-channel destinations also omit the heading even if they might be in the
  same workspace; proving that new binding is outside this minimal change.
- Render-time truncation respects 3000-character budgets without cutting escape
  entities or the short heading token. Long explanations retain their own sections;
  canonical indexed action values and returned choices are never rewritten.
- Outcome text (canonical answer and clicker's display name) is escaped and bounded
  only at `_update_clarify_message`; the already-rendered question stays byte-stable.
  Choice/Other/expiry updates add no active fallback mention, and duplicate taps do
  not update again. Legacy cards stay untouched until their existing normal action.
- Empty-choice Slack/base fallback behavior is deliberately unchanged. Batch semantics
  remain one initial heading per multi-choice prompt, not per option or update.

### TDD evidence

All test execution used `scripts/run_tests.sh`; transports were fakes and test homes
were temporary. No Slack API, credential, installed checkout or live profile mutation.

1. Before production edits:
   `scripts/run_tests.sh tests/gateway/test_slack_clarify_buttons.py -k 'intended_recipient or real_clarify_callback' --file-retries 0`
   returned **8 failed, 11 deselected**, all on absent intended raw heading syntax.
   The first run also exposed an incomplete DM fake channel map; it was corrected
   and the same command rerun to get eight intended assertion failures before GREEN.
2. Minimal heading/callback implementation: full clarify-button file **19 passed**.
3. New lifecycle/security/budget contract:
   `scripts/run_tests.sh tests/gateway/test_slack_clarify_buttons.py -k only_active_syntax --file-retries 0`
   returned **18 failed** on truncated entities. After bounded rendering it exposed
   a test harness access error (outcomes are context elements, not sections), corrected
   before rerunning: **18 failed** on raw injected mention syntax in outcome text.
   Escaping only fresh outcome text made the lifecycle contract pass.
4. The original five-file regression command returned **358 passed, 0 failed** at
   that stage (clarify-buttons 37, Slack 235, clarify-tool 48, clarify-gateway 27,
   timeout ambiguity 11). Diagnostic xfails were removed after the trusted contract passed.
5. Expanded real caller coverage uses actual `GatewayRunner._run_agent_progress_threading`,
   `TurnContext`, sync callback, cross-loop scheduling, registration and adapter rendering.
   Only adapter lookup/network transport/native stream consumer/human wait are substituted.
   It covers streaming on/off × channel/thread/DM × origin/home-chat/other-adapter/
   absent-workspace/foreign-workspace, including stale metadata that must not leak.
6. Broad Slack regression:
   `scripts/run_tests.sh tests/gateway/test_slack*.py tests/gateway/test_clarify_send_timeout_ambiguity.py tests/tools/test_clarify_tool.py tests/tools/test_clarify_gateway.py --file-retries 0`
   returned **644 passed, 0 failed across 38 files** (25.5 seconds); clarify-button
   file now has **61 passed**, including security, limits, canonical results and real
   caller metadata propagation. No retries or expected failures were needed.
7. After final test cleanup, the same 38-file command again returned **644 passed,
   0 failed** (75.2 seconds). Additional compatibility/control regression:
   `scripts/run_tests.sh tests/gateway/test_discord_clarify_buttons.py tests/gateway/test_telegram_clarify_buttons.py tests/gateway/test_clarify_active_session_bypass.py tests/gateway/test_clarify_progress_leak.py tests/gateway/test_clarify_thread_followup_not_swallowed.py --file-retries 0`
   returned **22 passed, 0 failed across 5 files** (3.1 seconds).
8. Final local checks, all exit 0:
   - `ruff check gateway/run_turn_runner.py plugins/platforms/slack/adapter.py plugins/platforms/slack/block_kit.py tests/gateway/test_slack_clarify_buttons.py`
   - `/Users/giovanni/.hermes/hermes-agent/venv/bin/python -m py_compile gateway/run_turn_runner.py plugins/platforms/slack/adapter.py plugins/platforms/slack/block_kit.py tests/gateway/test_slack_clarify_buttons.py`
   - `git diff --check`
   - Added-line static security scan: no credential-assignment, shell injection,
     eval/exec, pickle or SQL-interpolation findings. This is not independent review.
   - Final tracked diff: **290 insertions, 15 deletions across 4 files**, plus this
     untracked plan. All changes remain uncommitted on the original branch/base.

### Scope and remaining review gates

Changed production paths: `gateway/run_turn_runner.py`,
`plugins/platforms/slack/adapter.py`, `plugins/platforms/slack/block_kit.py`.
Test path: `tests/gateway/test_slack_clarify_buttons.py`. Plan path: this document.

Parent must obtain independent review before staging/publication. Full repository
suite, owner CI, live Slack rendering/notification receipt, non-macOS runs and fleet
activation are **not verified** here. Existing empty-choice/batch tests ran, but no live
batch notification test was performed. Review the conservative channel-map binding and
no-leading-token-normalization choices explicitly. Do not call local GREEN a rollout.

Reusable lesson: verify the actual transport's workspace selector, not merely a
metadata key; in this adapter a dedup-key flag also controls client selection. Prove
trusted-recipient propagation through the real callback with native streaming off,
where unrelated progress metadata does not contain recipient identity.

## Publication review and deployment gates

- Independent reviewer `sa-0-32c4dae9` returned `passed: true`, empty security concerns and logic errors. Nonblocking suggestions were additional boundary cases and live Slack render/notification verification. Parent independently reran the canonical clarification file: 61 passed, no expected failures.
- Publication agent reran the combined broad Slack and other-platform/control commands through `scripts/run_tests.sh --file-retries 0`: **666 passed, 0 failed across 43 files**, 59.2 seconds, no retries or expected failures. Changed-file Ruff and `git diff --check` passed. This supplements, not replaces, the earlier compile and static-security results.
- Verified fork `main` has no branch protection or required status contexts; branch rules endpoint returned an empty list. Actions are enabled. Actual PR check results must be read after publication; absence of a run must be reported as absent, never green. Merge only if observed checks and review gates permit it, without admin bypass.
- **Deployment blocked pending a user decision:** the standard product updater touches unrelated profiles, while the private infrastructure installer has no Hermes product component. A source-only fast-forward adaptation using the existing editable environment is a proposed alternative, not an approved rollout. Do not execute it or restart gateways. Preserve this worktree and branch pending deployment even after merge.
- No full repository suite, live Slack rendering/notification receipt, non-macOS run or fleet activation is claimed by the local test receipt. Exact remote PR and merged SHA belong in the publication readback, not a prediction in this pre-commit document.

## Closeout (implementation-stage receipt)

- Root-cause boundary and historical intent verified; local implementation is now tested, not deployed.
- Saved plan, regression tests and implementation in the isolated worktree; preserve all changes uncommitted for parent review.
- No merge/CI/readback or fleet convergence claimed; all rollout rows are pending or not applicable.
- No installed skill update: this task is restricted to the isolated worktree. Reusable diagnosis and implementation lessons are recorded here rather than modifying live profile state.
