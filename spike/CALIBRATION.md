# Calibration record — 2026-09-27, jev-1.13.0

Ground truth by rule, never by the model: removed parameters (callee == the finding's object and the
keyword is passed), removed objects (reference resolves through the consumer's imports and the library's
re-exports to the finding's module). Positional moves and "parameter added as required" are decided by
signature diffs in the engine and are not calibrated here. Evidence: `plain` = the call statement with 3
lines of context, comments stripped; `truncated` = only the line holding the keyword/name.

| set | labelled | reach acc @0.5 | reach ≥0.8 truly reached | reach <0.4 truly reached | break conf≥0.9 acc |
|---|---|---|---|---|---|
| httpx 0.27.2→0.28.0, plain | 40 (14+) | 40/40 | 13/13 | 0/25 | 14/14 |
| httpx, truncated | 40 | 38/40 | 13/14 | 0/23 | 14/14 |
| click 8.1.8→8.2.0, plain | 8 (7+) | 8/8 | 7/7 | 0/1 | 7/7 |
| click, truncated | 8 | 7/8 | 6/6 | 1/1 miss at <0.2 | 6/6 (one conf<0.5 answer, wrong) |

Findings that changed the design:
1. Evidence quality dominates calibration. With the callee visible the answer is bimodal and the
   0.2–0.8 band is nearly empty; with one line it is not, and each tail picks up ~1 error in 40.
   The renderer therefore always includes the whole call statement.
2. Two apparent model failures were question/label defects: "reach" is the wrong question for a
   positional move (the consumer never passes the moved keyword), and a removed `click.parser.Option`
   was being matched against `click.Option` (= `click.core.Option`) by both the label and the evidence.
   Both moved into deterministic code (`aftershock/positional.py`, `reach.resolve`).
3. Thresholds: REACH_YES 0.8, REACH_NO 0.4, BREAK_AUTO 0.8. Supported by the data above; re-run
   `spike/calibrate.py` after any change to the questions, the renderer, or the model version.

Cost: ~200 requests, ~$0.006.

## Thresholds, later revision

Pooled with 44 model-judged spans from an unpublished repository-history benchmark (truth = each commit's own diff; 92
spans in all), the thresholds moved to REACH_YES **0.7**, REACH_NO **0.4**, BREAK_AUTO **0.8**: every observation at or
above 0.7 was truly reached, none below 0.4 was, and every truly reached span was answered at break confidence >= 0.9.
The sections below re-check the engine on public repositories' histories, which anyone can reproduce.

## The settings-defaults row

A new environment-backed setting with a default, or a changed default, changes behaviour for every deployment that does
not set the key — and no signature changes, so no contract lens can see it. `pr-check` therefore lists every new or changed
default in a touched settings module (pydantic fields behind their env_prefix and aliases, rename-aware) with the charts that
set the key, and calls out a chart that pins the old value. Advisory (⚠️), never a verdict: only a person knows whether a
default is wrong for a deployment.

## 2026-10-02 public estate: pydantic and Prefect history (public repositories only)

`spike/history_bench.py` on public repositories (full clones, truth = the commit's own diff, rules plus the model,
$0.048 of model spend in total). The first run of each exposed rule gaps that earlier benchmarks never hit; each was
fixed by rule with a test, then the benchmark re-run.

| run | commits | verified | false | notes |
|---|---|---|---|---|
| pydantic, first | 24 | 31 | 17 (45% precision) | same-named members in the changed module (decode/encode/__hash__ on other classes), an unknown receiver the consumer also defines, a name still bound under `if TYPE_CHECKING` / `else` |
| pydantic, after | 24 | 10 | 0 (100%), recall 14/16 | the name still exported through `__all__` or a lazy table is a review item, never verified (the model, shown one statement, said reach 0.93) |
| Prefect, first | 16 (partial) | 165 | 8 | `serve()` imported from another module, `prefect.context.X` read as an instance, `build_image()` defined in the calling file |
| Prefect, after | 20 | 182 | 1 | the one is a real escape: `scripts/generate_cli_docs.py` imported `prefect.cli.root`, removed by the CLI promotion (#20838, 2026-02-25), repaired by #22503 on 2026-07-20 (145 days) |

Prefect without the bulk rewrite (`e977a2ca`, typer to cyclopts): 19 commits, 74 verified, 74 true, nothing unsurfaced.
The bulk commit alone: 108 verified, 107 true, 267 changed lines in the new CLI package never surfaced (modules replaced
wholesale; bulk rewrites are reported apart). Of 1,161 model-judged spans below reach 0.4, 67 sat on lines the commit
touched — the removed definitions themselves, which the model correctly calls not a use.

Rules added from this data: a module-level object reached through a receiver or an import must come from the finding's
module or a package above it; a receiver that resolves to a module of the library (`contracts.library_modules`) is never a
class instance; a member of the changed module's own name does not resolve by name; a member through an ambiguous
receiver is never decided by a positional rule; a 'removed' name still bound or exported at the new ref goes to a person;
a bare name the file defines itself is not the library's. Every one of these changes moves findings from verified to review,
never the other way.

Config lens on the public Helm chart (prefect-helm, `charts/prefect-server`): `read by code` 52 → 540 keys once an
`env_prefix` built by a helper function, a `...BaseSettings` subclass and `AliasChoices` are understood; 'set by chart, read
nowhere' fell from 11 to 1 (`HOME`).

## 2026-10-07 live dry-run sweep of public PRs, and the fix ladder

Every open, non-draft PR updated in the last 30 days that touches Python, at its latest commit, in PrefectHQ/prefect,
fastapi, pydantic, pydantic-settings, huggingface_hub, click and flask: 106 PRs, nothing posted. 0 verified breaks
after one false flag was traced and fixed: fastapi #16236 removes APIRouter.app because Starlette's Router, an external
base, provides it — three ❌ (self.app inherited from that base; route.app on fastapi's own APIRoute instances). Rules
added: a member removed from a class with a base outside the library is not verified gone; `self.x = ...` instance
attributes count when deciding that an unknown receiver is ambiguous. Also found by the sweep: packages deeper than two
directories (pydantic-core/python/pydantic_core) were never checked. 3 PRs carry review items only, which the
comment-only-where-it-breaks policy leaves silent.

History precision after the rules of 2026-10-06/07 (rules plus the model, truth = the commit's diff): flask 124/128,
huggingface_hub 41/41, pandas 11/11, werkzeug 364/373, click 109/109, Prefect 181/182 (the one is a real 145-day escape),
pydantic 10/10. flask's verified-true count fell 233 -> 124: 105 of the 109 were members moved into the sansio base in
1d8b53f7, lines the commit touched because it moved them, not because they broke.

Fixes (aftershock/fix.py proposes, aftershock/fixcheck.py climbs parses / resolves / clears / runs; spike/fix_bench.py hides one file's
edits from its commit and compares with what the author wrote). Per (commit, file), excluding files that define the
changed object: click 11 of 13 held up to runs, werkzeug 14 of 30, flask 4 of 18, Prefect 0 of 24 (19 rejected at
resolves: the CLI files import typer, which the change dropped), huggingface_hub 0 of 5, pandas 0 of 5 (partial: the
author also deleted a line). Of the fixes that held up to runs, few match the author exactly; most are working
alternatives (a newly private name, a kept alias the author deleted). Every rejection inspected was right.

## 2026-10-21 still broken at HEAD, and the rules it taught

`spike/head_scan.py` looks, at today's HEAD, for references to something an earlier commit removed or changed and that is
still gone. The first full run over the public estate reported 272 references in werkzeug, 8 in pydantic and 1 in fastapi.
None is a live break:

| hits | cause | rule |
|---|---|---|
| ~245 (werkzeug) | the builtin `set(...)` matched the changed `Headers.set` | a member is never reached by its bare name |
| 25 (werkzeug) | `url_map = Map(...)`; `url_map.add(...)` matched `Headers.add` | a receiver typed as another library class is ambiguous: a person decides |
| 1 (werkzeug) | `Authorization` moved to another module and kept `username` | a removed member is looked up where its class lives now |
| 7 (pydantic) | `pydantic/v1/mypy.py` keeps its own copy of the classes in `pydantic/mypy.py` | a class that defines the member itself, or a same-named class this file defines, is not the library's |
| 1 (pydantic) | the scan dropped a first parameter named `cls` from a module-level function | `self` / `cls` is dropped only for methods |
| 1 (werkzeug) | an example's `TemplateResponse.__call__` definition was judged as a call to `SharedDataMiddleware.__call__` | the scan judges calls only, never override definitions |
| 1 (fastapi) | the migration guide imports `fastapi.temp_pydantic_v1_params`, removed in 0.128.0 | none: the page documents 0.119-0.127 on purpose and no test runs the example |

After the rules, werkzeug and pydantic have 0 references left.

Rules-only history (no model, `--since 2023-01-01 --max-commits 40`, commits both versions scored), before -> after:
click true break lines 87 -> 88, false 0 -> 0; flask 74 -> 74, false 0 -> 0, review lines 317 -> 338 (members moved into
the sansio base, correctly left to review); werkzeug 319 -> 337, false 3 -> 0, review lines 971 -> 926. One fix found the extra
true breaks: when several classes change the same member, a reference is paired with the first finding it can reach (a
subclass's own override, the sansio class's own property), not only the first finding of that name.
