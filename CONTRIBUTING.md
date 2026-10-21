# Contributing to aftershock

Thanks for helping. aftershock has one promise: a ❌ means the line really breaks. Most of the work is keeping that
promise on code nobody has tried yet, so the most useful contribution is often a false positive you found, turned into a
test.

## Set up

```bash
git clone https://github.com/apoorv1997/aftershock
cd aftershock
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ",model" to also run the one test that needs the optional model SDK
python -m pytest -q tests          # no network, no API key; takes a few seconds
```

Python 3.11+. Nothing in `tests/` may touch the network or need `TYPESAFE_API_KEY`.

## The rule that matters most

**A wrong ❌ is worse than a missed one.** A rule change may drop a look-alike (a same-named object that is not the
changed API) or move a span from verified to review (❓). A change that turns review items into verified breaks needs
evidence: a benchmark run on a public repository showing those spans were real breaks.

## Found a false positive?

That is the best kind of issue. Open one with the **False positive** template, or fix it:

1. Cut the case down to the smallest tree: one or two files written in a test with `tmp_path`, and the `Finding` that
   matched (see the tests near the end of `tests/test_aftershock.py`).
2. Write the test first and check that it **fails** on `main`.
3. Fix it in the layer that owns the decision:
   - `aftershock/reach.py` when the span should never have been a candidate (wrong object, wrong class, wrong module).
   - `aftershock/rules.py` when it is a candidate but a rule decided it wrongly.
   - `aftershock/positional.py` when a signature rule misread a call.
4. Run the whole suite. If an older test now fails, check whether its fixture was unrealistic before changing it.

## Measuring a change on real history

`spike/` holds the benchmarks behind the numbers in the README. They run on local clones of public repositories:

```bash
git clone https://github.com/pallets/click ../click
python spike/history_bench.py ../click click --search src --since 2024-01-01 --out spike/click.jsonl
python spike/head_scan.py ../click click --search src --since 2024-01-01
```

`history_bench.py` asks, for every commit that changed a public API, whether aftershock flags exactly the lines that commit
had to change. Without the model extra, spans the rules cannot decide are counted as review items, so you can run it
without an API key. Results (`*.jsonl`, `*.log`) are gitignored; quote the numbers in your PR instead. A new library's
precision table is a welcome contribution on its own.

## Code style

- Plain names and simple control flow. Someone new should be able to read any function top to bottom.
- Short comments that say why. No dates, no story of how a bug was found: that belongs in the PR description or
  `spike/CALIBRATION.md`.
- Every rule gets a test that fails without it.
- Commit messages: `fix:`, `feat:`, `docs:`, `refactor:`, `build:`, `test:`, followed by what changed.

## Pull requests

- One change per PR. A rule fix and its test belong together; a refactor goes in its own PR.
- Say which repository or case motivated the change, and paste benchmark numbers before and after when a rule moves
  findings.
- CI runs the tests twice: without the model SDK and with it. Both must pass.

## Where help is wanted

- **New libraries.** Run the history benchmark on a library we have not tried and report what broke.
- **The routes lens** reads route decorators and FastAPI router prefixes. Starlette route lists, Flask blueprint
  prefixes and Django URL patterns are open.
- **The config lens** reads `os.getenv`, `os.environ` and pydantic settings. Other settings libraries are open.
- **A `fix` command.** Fix proposals and the hold-up ladder (`aftershock/fix.py`, `aftershock/fixcheck.py`) exist as a
  Python API, not yet as a CLI command.
- **Moved parameters.** `reach.py` treats any call with positional arguments as a candidate for a moved parameter.
  griffe's verbose output gives the old position, which would narrow that.

Look for issues labelled `good first issue` or `help wanted`, or open one to discuss a larger change before you start.

## Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through
[SECURITY.md](SECURITY.md), not public issues.
