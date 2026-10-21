# aftershock

[![tests](https://github.com/apoorv1997/aftershock/actions/workflows/tests.yml/badge.svg)](https://github.com/apoorv1997/aftershock/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Find the exact lines a library, API or config change breaks in other code — verified, with `file:line`, never a risk score.**

A signature diff tells you *what* changed. aftershock tells you *which lines elsewhere stop working*: in the same
repository, in the services that import the library, in the Helm chart that configures it, or in the TypeScript UI that
calls its routes. Rules decide everything they can; a calibrated model judges only what code cannot decide; anything still
uncertain goes to a person, never into a verdict.

```text
what changed (griffe API diff)  ─►  who might use it (AST candidates)  ─►  rules decide what rules can
                                                                                   │
                                                  a calibrated model judges the rest (optional)
                                                                                   │
                                     ❌ breaks · ❓ for a person · ✅ still valid — each with file:line
```

## Install

```bash
pip install "aftershock @ git+https://github.com/apoorv1997/aftershock"            # rules only
pip install "aftershock[model] @ git+https://github.com/apoorv1997/aftershock"     # + model judgement
```

Python 3.11+. The rules-only install depends on `griffe` alone; spans the rules cannot decide are listed for review.
The `model` extra adds TypeSafe's Jev, which returns probabilities rather than prose, for exactly two questions: does this
consumer really use the changed API (not a look-alike), and does the change break it. It needs `TYPESAFE_API_KEY`
(https://console.typesafe.ai/keys); every call is capped by a spend budget.

## Quickstart

```bash
# a library's API change -> the lines it breaks in a consumer tree
aftershock api-impact --lib ../httpx --package httpx --old 0.27.2 --new 0.28.0 --consumer ../my-service
aftershock api-impact ... --github --per-query 60 --max-queries 12      # public consumers via GitHub code search
aftershock api-impact ... --no-tests --format md --json out.json        # production code only, PR-comment markdown

# env keys the code reads (os.getenv, pydantic settings with env_prefix / aliases / helper-built prefixes) vs keys a chart sets
aftershock config-contract --code ./service --chart ./charts/service/values.yaml ./charts/service/templates

# HTTP routes a Python service exposes (FastAPI decorators + include_router prefixes, across files and repositories),
# old vs new, against URL literals in any consumer — a TypeScript UI included
aftershock routes --lib ../api --old <ref> --new <ref> --consumer ../web-ui --ext .ts,.tsx

# a named-query registry (function signatures served over HTTP) vs the payload keys its consumers send
aftershock named-queries --lib ../data-service --src app/queries --old <ref> --new <ref> --consumer ../reporting --no-tests

# one pull request, every applicable lens, one comment, driven by a manifest of which repositories consume which
# library and which chart configures which service; printed by default, --post sends it, --only-breaks posts only on ❌
aftershock pr-check --estate ~/.config/aftershock/estate.json --repo my-lib --pr 123
```

Spend caps: `--budget-usd` per run (default $0.02; skipped spans are reported for review, never guessed) and
`AFTERSHOCK_MAX_SPEND_USD` across runs (default $1), tracked in `~/.cache/aftershock/spend.jsonl`.

## What a report means

| mark | meaning |
|---|---|
| ❌ verified consequence | the code uses the changed API and the change breaks it — `file:line` and the finding |
| ❓ for a person | evidence exists but the receiver's type or the intent is unclear, or a cap stopped the call |
| ✅ still valid | the use was checked and still works after the change |
| not reached | a look-alike: the same name on a different object or library |

Production code is listed before tests; `--no-tests` drops tests. A new or changed settings default is reported as an
advisory ⚠️ row with the charts that set the key — no signature changes, so only a person can judge it.

## Proposed fixes, and whether they hold up

`aftershock.fix` proposes a fix for each broken file, most certain first: pass moved or keyword-only arguments by name
(behaviour-identical by construction); import a removed object from the one module that now defines it (exact only if its
definition is unchanged); or apply the edit the change itself made to its other callers. `aftershock.fixcheck` then climbs
a ladder and reports the highest rung the fix reaches:

| rung | passes when |
|---|---|
| parses | the patched file is valid Python |
| resolves | every library module and name it uses exists at the new version — and, for a file of the library's own repository, nothing it imports is a dependency the change dropped |
| clears | re-running the rules on the patched file finds no break of the change, and no new one |
| runs | the patched module imports against the new library |

A fix that deletes an argument, a call or a line is flagged as a **behaviour change** whatever it passes, and a fix that makes
code outside the library import one of its `_private` names is flagged too. "Runs" means it imports, not that it does what
the author intended. (Python API today; not yet a CLI command.)

## Evidence (public repositories, reproducible)

Measured 2026-10 on full clones; method and every number in [`spike/CALIBRATION.md`](spike/CALIBRATION.md).

**Precision on each library's own history** — for every commit that changed a public API, does aftershock flag exactly the
lines that commit had to change?

| library | verified true / verified | recall |
|---|---|---|
| click | 109 / 109 | 88% |
| pydantic | 10 / 10 | 88% |
| pandas | 11 / 11 | 100% |
| huggingface_hub | 41 / 41 | 89% |
| Prefect | 181 / 182 — the one is a real escape: a script imported a module a refactor removed, fixed 145 days later | — |
| werkzeug | 364 / 373 | 87% |
| flask | 124 / 128 | 81% |

**Live pull requests.** Every open, non-draft PR updated in the last 30 days that touches Python, at its latest commit,
across Prefect, fastapi, pydantic, pydantic-settings, huggingface_hub, click and flask: 106 PRs, nothing posted. No verified
break. The one false flag (fastapi #16236 removes `APIRouter.app` because Starlette's `Router`, an external base class,
provides it) became a rule, as did every false positive found on the way.

**Fixes.** Hiding one file's edits from its commit and asking for a fix: on click, 11 of 13 files got a fix that holds up to
*runs*. Few match the author's edit exactly — most are working alternatives — and every rejection inspected was right
(for example, an import fix for a script that also used a dependency the change had removed).

**Model calibration** on httpx 0.28 and click 8.2: with the whole call statement as evidence the reach answer is bimodal
(48/48 at 0.5, 50/50 on 14 adversarial cases — kwargs dicts, `partial`, wrappers, subclasses, aliases); a planted misleading
comment never flipped a verdict, so comments are stripped from evidence. Thresholds: reach ≥ 0.7 / < 0.4, break ≥ 0.8.

Reproduce: `spike/history_bench.py <repo> <package> --search src --since <date>`, `spike/pr_sweep.py --estate <manifest>`,
`spike/fix_bench.py <repo> <package> --rows <history rows>`, `spike/head_scan.py <repo> <package>`.

## Known limits

- **Python only.** The routes lens reads consumers in any language; the API lens reads Python.
- **Behaviour changes are invisible by design.** A new default, a changed return value or a slower query changes no
  signature; settings defaults get an advisory row, nothing more.
- **Unknown receivers** (`x.method(...)`) are matched by method name; when the consumer defines a same-named member itself,
  a "breaks" answer is capped to review. On very large PRs the review list can be long.
- **Members provided by an external base class** cannot be checked (its source is not in the repository), so their removal
  is never verified — it goes to review.
- A branch whose copy of a library is behind the trunk is API-compatible yet behaviourally stale; it is reported as a
  commits-behind note.

## Layout

```text
aftershock/contracts.py        griffe wrapper, Finding, re-export map, library module list, name bindings
aftershock/reach.py            AST candidates: imports and re-exports, receivers, aliases, scopes, ambiguity
aftershock/rules.py            the one deterministic router (CLI and benchmarks share it); inheritance and still-bound checks
aftershock/positional.py       signature rules: moved, newly required, kind-changed, removed-and-passed-positionally
aftershock/verify.py           optional model claims (reach, break) with calibrated thresholds; review on any failure
aftershock/fix.py              proposed fixes: keyword rewrite, moved import, analogy from the change's own edits
aftershock/fixcheck.py         the hold-up ladder: parses, resolves, clears, runs
aftershock/config_contract.py  env keys read (os.getenv, pydantic settings) vs set by a chart
aftershock/routes.py           HTTP route contract across files, repositories and languages
aftershock/named_queries.py    named-query registry signatures vs consumer payload keys
aftershock/prcheck.py          pr-check: one PR, every applicable lens, one comment
aftershock/explain.py          prose over verified consequences only (template / ollama / anthropic), with checks on the prose
aftershock/budget.py           per-run and cumulative spend caps
spike/                         benchmarks (history, PR pairs, fixes, still-broken-at-HEAD, PR sweep) and the calibration record
tests/                         rule-level tests, no network
```

## Contributing

Contributions are welcome, and the most useful one is often a false positive turned into a test. Found a ❌ that does
not break? Open a [False positive](https://github.com/apoorv1997/aftershock/issues/new?template=false_positive.yml) issue.
Want to add a rule, a lens or a benchmark on a new library? Start with [CONTRIBUTING.md](CONTRIBUTING.md): setup takes a
minute and the tests need no network.

License: MIT (see [`LICENSE`](LICENSE)).
