# Claude Rules

> **This file**: how Claude behaves in this repository — diagnostic
> disciplines, decision sequencing, verification, process guardrails.
> Architecture, code quality, and testing standards live in the docs
> linked below; this file points rather than restates.

## Where Things Live

| Topic | Authoritative doc |
| ----- | ----------------- |
| Architecture (SoC, DRY, Core/HA boundary, additive features, protocol primitives) | `packages/cable_modem_monitor_core/docs/ARCHITECTURE.md` |
| What enters Core's schema (fleet-observed metrics vs user analytics; signal-health-style proposals) | `packages/cable_modem_monitor_core/docs/ARCHITECTURE_DECISIONS.md` § Core Schema Model |
| Modem config principles (data-driven YAML, config-as-parameters, intake pipeline) | `packages/cable_modem_monitor_core/docs/MODEM_YAML_SPEC.md` § Principles |
| Code quality (no shortcuts, quality gates, forward refs, suppression discipline) | `docs/CODE_REVIEW.md` § Design Principles + Source File Standards |
| Test patterns (table-driven, fixtures vs inline, no data blobs, no modem-specifics, test overrides as smell) | `docs/CODE_REVIEW.md` § Test File Standards |
| HAR fixtures (Git LFS, `load_har_json()`) | `docs/CODE_REVIEW.md` § Loading HAR Fixtures |
| Logging conventions (event taxonomy, `log_event()`) | `packages/cable_modem_monitor_core/docs/LOGGING_SPEC.md` |
| Async / blocking I/O | `docs/CODE_REVIEW.md` § No Blocking I/O in Async Context |
| Release flow (branching, merging, tagging) | `docs/reference/RELEASING.md` |
| Process questions (where does X go? PR vs Discussion vs Issue?) | `CONTRIBUTING.md` |
| Specs by package | core: `packages/cable_modem_monitor_core/docs/README.md` · catalog tools: `packages/cable_modem_monitor_catalog_tools/docs/README.md` · HA: `custom_components/cable_modem_monitor/docs/README.md` · project: `docs/README.md` |
| Reference test (table-driven exemplar) | `tests/lib/test_parse_host_input.py` |

## Core Principles

These principles govern Claude's behavior on every change. They are
hard constraints, not guidelines. When in doubt, the principle wins
over convenience.

### Specs and Documentation

1. **Specs are the authority.** Code follows specs. No silent
   deviations. If the code needs to diverge, discuss the gap first,
   update the spec, then update the code.

2. **Design decisions land in specs, not in conversation.** Every
   architectural decision made during a session must be committed to
   the relevant spec file before the session ends. Conversation
   history is ephemeral — specs are durable.

3. **Docs and code move together.** Every core change reconciles the
   affected specs (ARCHITECTURE, ORCHESTRATION_SPEC, MODEM_YAML_SPEC,
   etc.). A code change without a corresponding spec update is
   incomplete.

4. **Write for clarity and brevity — then cut again.** Before
   committing any prose (spec text, generated-doc copy, comments,
   commit bodies), reread it and delete what does not earn its place:
   throat-clearing openers ("A third situation is…"), restated
   context, hedges, and clauses that repeat a neighbouring sentence.
   Say the thing once, in the fewest words that keep it true. A
   definition a reader has to parse twice is a definition that will
   get asked about again. This applies to the *first* draft, not a
   later polish pass — verbose text ships and then nobody trims it.

### Two READMEs — GitHub vs HACS (do not consolidate)

The repo intentionally ships two README files for two render
surfaces. They are not duplicates to merge:

- `.github/README.md` is the **GitHub landing page** (GitHub serves a
  README from `.github/` in preference to the repo root). Relative
  links in it resolve from `.github/`, so a `./docs/X` resolves to
  `.github/docs/X` and 404s — use links that resolve from `.github/`,
  or absolute
  `https://github.com/solentlabs/cable_modem_monitor/blob/main/...`
  URLs.
- The root `README.md` is what **HACS** renders in its panel. HACS
  does not resolve repo-relative paths, so this file must use
  **absolute** URLs only.

When editing either, keep the distinction and the per-file link rule.

### Process

4. **Only the developer stages files.** Never run `git add`. Show
   the list of changed files and proposed commit message. Let the
   developer stage them.

5. **No external actions without discussion, per action.** Never
   create GitHub issues, PRs, commits, pushes, label changes, or any
   external-facing action without explicit discussion first.
   Approval of a *plan* containing an external action is not approval
   of the action — confirm again immediately before executing it.
   Iterating on draft text ("how about X", "change Y to Z") is
   drafting, not authorization, even when the developer supplies the
   final wording; only "post it" / "send it" authorizes the call.
   Local actions (edits, tests, lint) inherit plan-level approval
   normally.

6. **Before deleting or moving ANY file, run `rg <filename>` across
   the entire project.** Files are referenced by non-Python sources
   (CI workflows, Makefiles, docs, VS Code tasks) that linters don't
   scan. When any task label, script name, or path changes in
   `.vscode/tasks.json`, also audit: `scripts/dev/next_steps.txt`,
   `scripts/dev/welcome_message.txt`, `.devcontainer/post-start.sh`,
   `docs/setup/GETTING_STARTED.md`. Task name drift is invisible to
   linters and causes silent breakage in the contributor on-ramp.

7. **Always read a file before writing to it. No exceptions.** Even
   "I just want to overwrite it" — read first. Local-only/gitignored
   files especially: no git recovery path. The Write tool errors if
   you skip the read; do not work around it.

8. **Stop on placeholders.** When reading code, config, or YAML
   during analysis, halt and flag immediately on `XXX`, `TODO`,
   `FIXME`, `TBD`, `???`, `undefined`, `placeholder`, `replace_me`.
   Do not summarize the surrounding architecture as "looks good"
   while quietly ignoring unfilled values.

9. **Don't offer "revisit later" as an option.** When presenting
   design choices, offer "ratify now" or "drop the idea entirely."
   Never present "keep the ambiguity and revisit later" as a third
   option — deferred items pile up and silently expire.

10. **No "pre-existing" framing.** Don't dismiss code gaps as
    "pre-existing," "not mine," or "from an earlier session." The
    full working tree is in scope unless explicitly narrowed. The
    only valid scope-narrowing reason is *what* the gap is, never
    *who wrote it first*.

11. **Don't claim unverified fixes** in user-facing replies (GitHub
    issues, comments). Use hedged language: "should address," "ready
    to test," "if it works, please post diagnostics." Only claim
    "fixed" after the user confirms on their hardware.

12. **Never read the HA test config `.storage` directory.** The path
    is denied in `.claude/settings.json` (`permissions.deny`) and
    mounted under the `/config` volume in `docker-compose.test.yml`.
    It contains live modem credentials in plaintext (HA stores
    config-entry data unencrypted on disk by design). Reading it via
    any tool — Read, `cat`, `grep`, `rg`, `jq`, `awk`,
    `python -c "open()"` — leaks the password into the conversation
    context. The settings.json deny only blocks the Read tool; this
    rule covers the rest. If you need config-entry fields for
    analysis, ask the user to paste a redacted excerpt.

## Diagnosis Discipline

When a runtime error appears in user-supplied logs, **ask for the data
that would distinguish candidate causes before generating theories.**
Typically the surrounding ±10 log lines. Don't theorize first; don't
propose fixes first.

- **Differential test**: every theory must answer "why now and not
  before?" If it can't, it's incomplete — don't commit to a fix
  built on it.
- **User hypotheses are primary evidence**, not options among yours.
  Tentative phrasing ("if we... maybe this...") doesn't downgrade
  the signal — the user has runtime context the codebase doesn't.
- **External failure modes are invisible to grep.** Install path,
  network path, runtime config, user actions — none of those show
  up in codebase searches. When stuck inside the repo, ask: "could
  this be coming from outside the code?"
- **Don't propose fixes until you can name what specifically broke
  and why.** "Probably X" is not a fix-ready diagnosis.
- **When the task cannot validate a hypothesis, the deliverable is an
  evidence ledger, not a recommendation.** Some problems have no
  reproduction, no hardware, and no way to test a theory locally
  (#120: five months, seven contributor retests, a dozen dead
  theories). In that state every session invents a fix and every fix
  collapses under the next question. Report what is closed and what is
  open, and stop. A theory closed with evidence is worth as much as a
  change and is the only progress such an issue accepts — the durable
  output of the 2026-07-27 session was its negative results, not one
  of its recommendations.

## Decision Discipline

- **One thing at a time.** Surface decisions sequentially; don't
  dump 6-row tables of "outstanding work." Long synthesized lists
  are too much to absorb in one pass and let shortcuts slip
  through.
- **Research returns a recommendation, not a paper.** When asked to
  research, analyze, or assess, default to a 2–3 sentence answer
  with the single tradeoff that matters. Tables, section headers,
  Phase-numbered plan scaffolding, ASCII diagrams, and leverage
  rankings are opt-in — only expand when the user asks "explain
  why" or "show your work." This rule exists because research
  prompts repeatedly returned multi-section papers when a
  recommendation was wanted.
- **Structure over presentation.** Prioritize the data model and
  schema; defer presentation polish until the structure is settled.
- **No judgment shortcuts.** Don't dismiss alternatives with
  "overkill," "churn," "make-work," or "no cohesion payoff" without
  weighing real costs and benefits. The shortcut costs more later —
  either a missed improvement or a re-litigated decision.
- **Know what you know — don't speculate.** Model what we actually
  observe; stop there. Don't add inference-based features when the
  signal is ambiguous (multi-signal voting, tunable thresholds, etc.
  are tells).
- **Park side investigations.** When a parallel audit returns
  results, summarize and surface as a *separate* task. Don't merge
  the punch list into the active commit batch without explicit
  ratification.
- **Avoid refactor thrashing.** After 1–2 "this smells" rounds on
  the same module, stop and ask for the end state. If rounds 1 and
  2 haven't converged, round 3 won't either — the underlying issue
  is the goal isn't clear, not that the current location is wrong.
- **Don't defer obvious cosmetic fixes.** If a review surfaces a
  real issue (stale name, drifted docstring, minor nit), fix it in
  the current pass. *"Whenever we say we should take care of
  something later, we do not, and that adds to hidden tech debt."*
  Never write "separate pass if desired" — that's deferral dressed
  as a suggestion.
- **Name the governing spec before recommending anything in its
  subsystem, and say in the reply which one you read.** Code plus
  tests is not a substitute. The `AUTH_LOCKOUT` mapping was assessed
  from the signal enum, the HA error map and 12 locale files, and the
  recommendation was wrong: the real finding was in `AUTH_HNAP_SPEC.md`,
  which was never opened. Stating the spec out loud makes the omission
  visible at a glance instead of costing a round trip to discover.
- **Don't manufacture urgency, and don't dismiss by authorship.**
  Inflating a remote edge case into a release gate and waving a real
  gap away as "pre-existing" are the same move: closing a question
  instead of weighing it. Rank a gap by what it is and what it costs,
  then say plainly whether it blocks the current cut.

## Verification Discipline

The first three rules govern the rest of this section. Every other rule
here asks for a check done privately; these three put the check in the
reply, where a skipped one is visible without re-running the work.

- **Cite what you opened.** When stating a fact or a cause about the
  system, name the files or commands the claim rests on. A claim about
  CI cites a workflow file; a claim about history cites `git log`. When
  the named sources don't cover the claim's domain, the hole shows on
  the page: *"CI isn't recording the trend (checked: Makefile, the
  regression script)"* is visibly unsupported, because neither source
  is CI. This is the forcing function from *name the governing spec*,
  generalised past specs.
- **An unexplained number stays unexplained.** Report the measurement;
  do not supply a cause you have not verified. "I can't account for
  this yet" is a complete answer. On 2026-07-29 every measurement was
  correct — fleet accuracy, the per-modem deltas, the four-day zero
  window — and every wrong conclusion was an invented cause laid over
  one of them, each disproved by a single `git log` or `grep` that ran
  only after the user pushed back.
- **Review your own diff before declaring it done.** For any change to
  runtime behavior (policy, auth, orchestration, recovery), re-read the
  full diff as a reviewer hunting for what it breaks, not as the author
  confirming it works. Name in the reply the failure mode you looked
  for. Green tool gates are not this check: black, ruff, mypy, pyright,
  pre-commit and `validate-ci` were all green on the #185 auth change
  while it would have posted credentials six times at an unknown device
  and then shown the user the wrong remedy. Both defects were found only
  because the developer asked for a review, which is not a gate.

- **Verify against ground truth, not against doc claims.** When
  asked to review a planning doc / status doc / roadmap, summarize
  what's *actually true* (check code, git, issues), not what the
  doc *says*.
- **Empty output is not an empty set.** Never assert absence from a
  command whose output you haven't confirmed is well-formed. A `--jq`
  expression that silently emits nothing is indistinguishable from a
  real zero: totev#313 was reported as "closed same day, no comments,
  no fix" and made a session's headline conclusion, when it in fact
  had two comments that reversed the reading entirely.
- **Verify the premise before creating a worktree.** For any task
  that says "remove X" or "clean up Y," `rg` for it on the current
  branch first. Zero hits means the work is already done — stop
  before spinning up a worktree.
- **After any hand-off, audit the full diff before touching
  anything.** Run `git diff HEAD --stat` and review every changed
  file. The gitStatus snapshot in the session header can be stale,
  and another session on the same worktree may have regressed
  earlier work.
- **Never spawn a sub-agent to implement a feature that touches
  existing code, specs, or tests.** Sub-agents lack project history
  and make unsolicited "cleanup" decisions — removing fields,
  dropping test coverage, stripping documentation — on things they
  don't understand. The only safe scope for a sub-agent is narrowly
  bounded read-only research. If context window pressure makes
  direct implementation feel necessary, break the task into smaller
  sessions instead.
- **Recurring problem = root cause unfixed.** If a fix has to be
  re-applied within one session, stop fixing the symptom and find
  what's recreating the failure.
- **Never dismiss test failures as "pre-existing."** If tests pass
  on committed code but fail with working-tree changes, it's a
  regression. Stash and verify on clean state before claiming
  pre-existing.
- **Done means done.** When a task is class-scoped ("all issue
  templates," "all parser docstrings"), apply the criteria
  consistently to every member. Pass-through skimming for one
  specific issue isn't done.
- **Commit before recording done.** Work must be committed to a
  durable branch before journal/memory says "done," "added," or
  "implemented." Stashed work on a worktree branch is one
  `git gc` away from loss.
- **Run pyright alongside mypy.** `mypy` and Pyright (Pylance) have
  different strictness. Code passing mypy can still show red
  squiggles in VS Code. After mypy, run
  `PYRIGHT_PYTHON_FORCE_VERSION=latest .venv/bin/pyright` over
  **every file in `git status`, tests included** — Pylance tracks
  latest pyright, and touched test files are where missed
  diagnostics repeatedly surface. Do this unprompted before
  declaring any work unit done, in the same pass as the test run.
- **Preserve actor when restating prior facts.** When summarizing or
  recommending based on a prior exchange, the subject/object of "who
  said/did/decided X" is load-bearing. Compressing "X reported Y"
  into "we told X about Y" (or vice versa) misrepresents the record
  even when the surrounding argument is sound. Re-read load-bearing
  sentences against the actual exchange before submitting.
- **Verify factual claims before posting externally, even when
  user-supplied.** The claim originating from the user doesn't exempt
  it. Public channels (GitHub issues, PR comments) require the same
  ground-truth check as claims generated here.

## Catalog & Data Discipline

- **HAR captures are immutable evidence.** Never edit one — broken
  markup is firmware behavior the parser must handle. Full policy:
  `packages/cable_modem_monitor_catalog_tools/docs/MODEM_INTAKE_WORKFLOW.md`
  § HAR Captures Are Immutable Evidence.
- **No modem-specific behavior in `modem.yaml`.** Config selects and
  parameterizes Core behaviours; no behavior flags, no per-modem
  timing knobs. Authority: `MODEM_YAML_SPEC.md` § Principles.
- **Recovery logic stays generic.** All triggers reach the same code
  path; no per-modem recovery tuning. Authority:
  `ORCHESTRATION_SPEC.md` § Recovery.
- **HAR intake is the only data path.** When a user's modem isn't
  matching the catalog, the default ask is a fresh HAR — not
  one-off questions ("what URL is in your address bar?"). HAR is
  reproducible, auditable, and feeds the catalog_tools pipeline.
  Fall back to direct questions only if `har-capture` genuinely
  can't capture what's needed.
- **Verified JSON must be faithful.** `modem.verified.json` is a
  faithful copy of the diagnostics `data` section, not a curated
  subset. Strip list and format:
  `packages/cable_modem_monitor_catalog_tools/docs/MODEM_INTAKE_WORKFLOW.md`
  § Build verified.json.
- **Source all factual claims.** Every factual claim in data files
  (`providers.json`, `chipsets.json`, modem.yaml notes/sources)
  must include a reference URL or citation. Without a source, the
  claim is indistinguishable from fabricated data. If a source
  can't be found, leave the field empty rather than guessing.
- **`packages/cable_modem_monitor_catalog/README.md` is auto-generated.**
  Never edit it directly. Run `python3 packages/cable_modem_monitor_catalog/scripts/generate_catalog_index.py`
  to regenerate.

  Three rules: (1) Contributors are not responsible for
  regenerating it — the `/modem-confirm` and `/modem-intake` skills handle
  it as a verified final step and may bundle it with the catalog commit.
  (2) When multiple catalog changes land in one session, regenerate once
  after all changes are staged, not per-change. (3) CI gates on README
  freshness — if a PR fails this check, regenerate and amend before merging.
- **Catalog data stays true to source; normalization happens at
  presentation.** Manufacturer styling variation is real signal, not
  drift — never pre-normalize in the catalog; display layers own
  case normalization. Authority: `ARCHITECTURE_DECISIONS.md`
  § Core Schema Model → Catalog data stays true to source.

## Code Discipline

- **TDD for non-trivial bug fixes.** (1) Read relevant specs.
  (2) Document the use case if missing. (3) Write tests that fail.
  (4) Implement. (5) Verify tests pass.
- **Fix the actual bug — no speculative migrations or renames.** AI
  drift is dangerous: a fix that "also cleans up" adjacent naming,
  structure, or config it wasn't asked to touch creates review
  burden and regressions. Scope the diff to the defect.
- **One-line docstrings only.** Never multi-paragraph docstrings
  with Args/Returns/Raises sections — the signature and type
  annotations carry that information. One short line max. Non-obvious
  WHY (hidden constraints, caller contracts, mutation side effects)
  goes to inline comments co-located with the relevant code.
  Behavioral contracts and design decisions belong in the spec docs
  (ORCHESTRATION_SPEC, MODEM_YAML_SPEC, etc.) — they are the
  durable documentation layer, not docstrings.
- **Keep WHY comments on refactor.** Don't strip section markers
  (`# Phase 1 — auth`), rationale notes, or numbered-procedure
  markers during a rewrite. The "default to no comments" rule
  targets WHAT-noise, not WHY-context.
- **No em-dashes in code comments.** Punctuate with commas,
  semicolons, or periods, or split the sentence. Same instinct as
  the contributor-comms rule below, extended to code. Applies to
  comments being written or already under edit; don't rewrite
  existing comments solely for this.
- **Isolate before sprawl.** If a feature would touch >2 files,
  it probably needs its own module. Spreading wiring across
  `button.py`, `sensor.py`, `coordinator.py`, and `__init__.py`
  for one concern is the smell.
- **No infrastructure for hypothetical recurrence.** Before adding
  a test, CI job, hook, script, or module to address a one-shot
  incident, state the problem in one sentence and ask: am I
  protecting against documented past failures, or against a
  hypothetical future one? If hypothetical, name it as such and
  let the user choose whether to invest. A wrong command in one
  GitHub comment doesn't justify a smoke test + Make target + CI
  job; a doc fix or upstream link does.
- **Small files are fine** when (a) the logic is clearly bounded
  (one concern, one reason to change) and (b) the file has a clear
  docstring explaining what lives there. A 50-line file with one
  clear concern beats a 50-line addition to a `utils.py` dumping
  ground.
- **Type-safety patterns.** Preferred idioms (`Literal` over `str`
  enums, `model_validate` over `Model(**data)`, `lru_cache` over
  module-global caches, etc.): `docs/CODE_REVIEW.md` § Type Hints.
- **Use existing pipelines.** Never hand-build artifacts when a
  pipeline tool exists. `generate_golden_file()` runs the real
  parser coordinator. Serialize with
  `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False) + "\n"`.
- **Docstring placeholders.** In docstring examples, use template
  placeholders (`{manufacturer}/{model}/`), not specific fake names
  (`acme/a100/`). Tests still use concrete strings; docstrings
  describe the pattern.
- **No P-numbers in public artifacts.** Roadmap identifiers (`P28`,
  `P34`, etc.) come from an internal roadmap doc that ships only
  locally. Tag annotations, CHANGELOG entries, GitHub release notes,
  and issue replies cite GitHub issue numbers, not roadmap Pxx.

## Shell Command Generation — Avoid Permission Check Triggers

When generating shell commands:

1. **Never embed newlines or `#` characters inside quoted strings** passed as command arguments
2. **For multiline shell logic**, write to a `.sh` script file first, then execute the file
3. **Prefer simple, single-line commands** with explicit arguments
4. **For JSON parsing**, use `gh`'s built-in jq engine via `--jq`/`-q`. The standalone `jq` binary is **not installed** — piping to it fails with `command not found`, and inside a pipeline that failure is silent. Never pipe `gh` output to `python3 -c`. Example: `gh issue view 152 --json title,body -q '.title, .body[:800]'`
5. **For JSON that does not come from `gh`**, or logic `--jq` cannot express, write to a fixed path `/tmp/claude_parse.py` (overwrite each time, not per-invocation) and run `python3 /tmp/claude_parse.py`
6. **Before executing any shell command**, verify it contains no newline characters inside quoted strings and no `#` characters that could be interpreted as hidden arguments

**Why?** Embedded newlines break allowlist pattern matching — `Bash(gh issue view*)` will not match a command containing newlines, so the permission prompt fires even for explicitly allowed prefixes. `--jq` sidesteps this entirely.

## Pre-Push Verification — ALWAYS Run Before Push

Before pushing ANY commits, run the canonical local CI mirror:

```bash
make validate-ci
```

This runs lint + format + type-check + tests + intake regression +
PII check + catalog README freshness — the same surface CI's Tests
workflow exercises. `scripts/release.py` runs it automatically before
every version bump.

**Why?** CI runs on the entire project. Pre-commit hooks only check
staged files, and `make test` is a subset of CI.

**Review the push range, not just your own commit.** Run
`git log @{u}..HEAD` first. Commits made outside this session ride
along and hit CI under your push; their failures land on your commit.
Flag anything in the range you did not verify.

**`validate-ci` green does not guarantee CI green.** It mirrors what
CI runs, not where CI runs it. Per-job settings in
`.github/workflows/` — `lfs:`, path filters, fresh-clone state — are
invisible locally, where the repo is complete and LFS content always
present. When a change alters what a script *reads*, check the
checkout step of the job that runs it.

**Owned-deps check:** `validate-ci` ends with `scripts/check_owned_deps.py`,
which reports only packages declared in our requirements files and
pyproject.toml — not the transitive HA or test-harness tree. When it
shows drift, propose a separate deps-update commit before pushing.

**HA compatibility gate:** `validate-ci` runs `scripts/check_ha_compat.py`
(mirrored by the `ha-compat-check` CI job), which validates that every floor
declared in Core and Catalog's `pyproject.toml` is satisfiable under HA's
`package_constraints.txt`. This is a hard gate — exit non-zero blocks the
push. Never bump a floor in a published package's pyproject.toml above what
HA constrains without first verifying compatibility (the beta.4 incident:
`requests>=2.34.2` and `pyyaml>=6.0.3` both exceeded HA's pins).

**CI job coverage:** When verifying CI after a push, confirm every
expected job ran. A missing job is not a pass — absence of failure is
not success. If a job didn't trigger, check the workflow's path filters
and fix them before declaring the push clean.

### Optional pre-push hook — opt-in, suggest at the right moment

`make install-hooks` installs an opt-in `.git/hooks/pre-push` that
runs `make validate-ci` automatically before every push (chained
after `git lfs pre-push`). It is not committed-by-default — CI is the
authoritative gate, and forcing the hook on every developer would
create install-state inconsistency without adding any enforcement CI
doesn't already provide.

**When to suggest it**: after a developer hits a CI failure that
`make validate-ci` would have caught locally (coverage drop, lint
miss, missing local-mirror), suggest `make install-hooks` *once*. Do
not repeat the suggestion if they decline, and do not suggest it on
fresh clones or on every validate-ci run — that becomes noise.

### Adding a new CI job — local-mirror rule

**Whenever you add a new job or step to `.github/workflows/tests.yml`,
add a corresponding Makefile target and wire it into `make
validate-ci` as a dependency.** Every CI check must have a
local-mirror command. The two together are a single change, not a CI
change with a "follow up" Makefile change. Drift between CI and local
is what hides regressions until tag time (see alpha.17 retrospective).

If the job name is listed as a required status check in the
`require-status-checks` repository ruleset, update the ruleset at the
same time. The ruleset is a plain string match — it has no awareness
of the workflow files. Rename drift silently breaks every subsequent
PR (shows "Expected — Waiting for status to be reported" on required
checks). Update via `gh api repos/solentlabs/cable_modem_monitor/rulesets/10547747 --method PUT --input <payload>`.

Exceptions: external GitHub Actions that can't be reasonably
reproduced locally (e.g., `home-assistant/actions/hassfest@master`,
which requires HA core source and Docker). Document the exception in
the Makefile comment so future-you knows why `validate-ci` doesn't
cover it.

## Irreversible Operations — STOP and VERIFY

When the user gives explicit constraints (e.g., "without closing the PR", "don't delete X"):

1. **Treat these as HARD BLOCKERS** — not suggestions
2. **Research/verify the outcome BEFORE executing** — if unsure, ASK first
3. **If something goes wrong, STOP and ask** — don't try to fix it autonomously
4. **Never assume** — GitHub branch renames, force pushes, deletions can have cascading effects

Examples of irreversible operations requiring verification:

- Branch renames, deletions, force pushes
- PR/issue closures
- Tag deletions
- Any git operation with `--force`

## Contributor Communications

Voice and content rules for anything posted to GitHub (issues, PRs,
discussions, release notes) or other public surfaces.

- **Solo-maintainer voice.** Ken writes as "I", never "we/us/our".
  There is no team.
- **Humble, short, personal.** First names in greetings; @-mention
  only when the comment must notify. No usernames-as-names.
- **No LLM tells.** No "Good news:", no "just landed", no em-dashes,
  no stylistic hyphens, no "/" separators in prose.
- **Claims must be referenced.** State only what's verified;
  inferences get "one possibility is...", never stated as diagnosis.
  Drop speculative "this also helps X" claims. User-supplied claims
  get the same ground-truth check before posting.
- **Attribute Claude.** Never write "I read the code" in Ken's voice
  if Claude did the analysis.
- **Post drafts verbatim.** When Ken supplies reply text, post it
  unmodified — no padding, no fluffing.
- **Acknowledge input already given.** Reflect a contributor's
  specific ask back in their own terms; don't re-ask for what they
  already provided, and skip install/setup walkthroughs for
  returning contributors.
- **No contributor details in code.** No handles or literal user
  inputs in comments, tests, or specs — cite issue numbers, use
  generic values.

## PR and Issue Conventions

No auto-close keywords (`Fixes #X`, `Closes #X`, `Resolves #X`) in PR
bodies *or* commit messages — GitHub scans every commit in a merge
and closes regardless of qualifier. Use `Related to #X` /
`Addresses #X` instead.

Before authorizing any merge to main, scan the whole merge range and
flag every match to the developer as a blocker:

```bash
git log <base>..<head> --format='%H%n%B%n---' | grep -inE '(clos(e|es|ed)|fix(es|ed)?|resolv(es|ed)?)[[:space:]]+#[0-9]+'
```

The parser doesn't read English: "would resolve #X if…" closes it, and
so does "doesn't fix #X". This bit PR #145, where "still required to
confirm fix resolves #81 specifically" auto-closed #81 on merge and it
had to be reopened by hand. Applies equally when Claude authored the
offending commit in an earlier session.

Issue label glossary and state semantics live in
[CONTRIBUTING.md § Issue Labels](CONTRIBUTING.md#issue-labels) and
[CONTRIBUTING.md § Issue Closing Policy](CONTRIBUTING.md#issue-closing-policy).
