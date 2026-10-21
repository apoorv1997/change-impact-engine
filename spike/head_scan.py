#!/usr/bin/env python3
"""Still broken at HEAD: for every commit since a date that removed or changed a public API, look for references that
TODAY's code (the repository at --head) still makes to what that commit removed. A reference
counts only when the object is still gone at HEAD (not re-added, not provided by a base class, not re-exported), and
every hit is then imported at HEAD to see whether it raises.

Usage: head_scan.py <repo> <package> [--search src] [--since 2025-01-01] [--max-commits 80] [--head origin/main]
                    [--python ENV_PY] [--budget-usd 0.05] [--out spike/head-<repo>.jsonl]"""
from __future__ import annotations

import argparse, concurrent.futures as cf, json, pathlib, subprocess, tempfile

from aftershock import budget, contracts, evidence, fixcheck, positional, reach, rules, verify

KINDS = ("Parameter was removed", "Public object was removed", "Positional parameter was moved",
         "Parameter was added as required", "Parameter kind was changed")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def binds(code: str, fn, names: set[str], method: bool = True) -> bool | None:
    """Does the call in `code` bind to `fn`'s signature (today's)? None when it cannot be told (*args / **kwargs at the
    call). A call written after an old commit changed the signature is judged against HEAD, never against that change.
    `method` is False for a module-level function, whose first parameter is passed even when it is named `cls`."""
    import ast
    node = positional.parse_stmt(code)
    call = next((n for n in ast.walk(node) if isinstance(n, ast.Call) and reach._callee(n) in names), None) if node else None
    if call is None or any(isinstance(x, ast.Starred) for x in call.args) or any(k.arg is None for k in call.keywords):
        return None
    a = fn.args
    pos = a.posonlyargs + a.args
    if method and pos and pos[0].arg in ("self", "cls"):
        pos = pos[1:]
    names_pos = [x.arg for x in pos]
    n = len(call.args)
    if n > len(names_pos) and not a.vararg:
        return False                                            # too many positional arguments
    filled = set(names_pos[:n])
    kw_ok = {x.arg for x in a.args + a.kwonlyargs} - {"self", "cls"}
    for k in call.keywords:
        if k.arg in filled or (k.arg not in kw_ok and not a.kwarg):
            return False                                        # given twice, or no such keyword (or positional-only)
        filled.add(k.arg)
    n_def = len(a.defaults)
    required = names_pos[: len(names_pos) - n_def] if n_def else names_pos
    required += [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return all(r in filled for r in required)


def is_override(code: str) -> bool:
    """Is the span a method definition (an override) rather than a call? This scan judges calls only."""
    return code.lstrip().startswith(("def ", "async def "))


def class_files(lib, head, cls: str) -> list[str]:
    """Python files at `head` that define `class <cls>` at module level."""
    out = git(lib, "grep", "-l", "-E", f"^class {cls}[(:]", head, "--", "*.py")
    return [line.split(":", 1)[1] for line in out.splitlines()]


def still_gone(f, lib, head) -> bool:
    """Is what the old commit removed or changed still removed or changed at HEAD?"""
    if "Public object was removed" in f.message:
        if f.object_name == "<module>":
            return subprocess.run(["git", "-C", lib, "cat-file", "-e", f"{head}:{f.path}"], capture_output=True).returncode != 0
        if "." in f.object_name:
            if rules._show(lib, head, f.path) is None:
                # the module itself moved: the class may live on elsewhere with the member
                return not any(rules.inherited(contracts.Finding(p, f.line, f.symbol, f.message), lib, head)
                               for p in class_files(lib, head, f.object_name.split(".")[-2]))
            return not rules.inherited(f, lib, head)
        src = rules._show(lib, head, f.path)
        return src is None or not contracts.binds(src, f.object_name)
    if "Parameter was removed" in f.message:
        fn = positional.positional_params(lib, head, f.path, f.object_name)
        req = positional.required_params(lib, head, f.path, f.object_name)
        return fn is not None and f.parameter not in (fn or []) and f.parameter not in (req or [])
    return True          # moves / kind changes / newly required: the HEAD signature decides via the rules below


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("package"); ap.add_argument("--search")
    ap.add_argument("--since", default="2025-01-01"); ap.add_argument("--max-commits", type=int, default=80)
    ap.add_argument("--head", default="origin/main"); ap.add_argument("--python"); ap.add_argument("--budget-usd", type=float, default=0.05)
    ap.add_argument("--out"); ap.add_argument("--workers", type=int, default=5)
    a = ap.parse_args()
    pkg_dir = f"{a.search + '/' if a.search else ''}{a.package}"
    commits = git(a.repo, "log", "--first-parent", "--format=%H %ad %s", "--date=short", f"--since={a.since}", a.head, "--", pkg_dir).splitlines()
    print(f"{len(commits)} commits since {a.since} touch {pkg_dir}", flush=True)

    def findings_of(line):
        sha = line.split(" ", 1)[0]
        try:
            return line, [f for f in contracts.griffe_findings(a.package, sha + "^", sha, cwd=a.repo, search=a.search, timeout=300)
                          if any(k in f.message for k in KINDS)]
        except subprocess.TimeoutExpired:
            return line, []
    with cf.ThreadPoolExecutor(a.workers) as pool:
        changed = [(l, fs) for l, fs in pool.map(findings_of, commits) if fs][: a.max_commits]
    print(f"{len(changed)} commits changed a public API", flush=True)

    out = open(a.out or f"spike/head-{pathlib.Path(a.repo).name}.jsonl", "w")
    seen, hits = set(), 0
    with tempfile.TemporaryDirectory(prefix="aftershock-head-") as root:
        subprocess.run(f"git -C {a.repo} archive {a.head} | tar -x -C {root}", shell=True, check=False)
        head_sha = git(a.repo, "rev-parse", a.head).strip()
        exports = contracts.package_exports(a.repo, a.package, a.head, a.search)
        modules = contracts.library_modules(a.repo, a.package, a.head, a.search)
        for line, fs in changed:
            sha, date, subj = line.split(" ", 2)
            gone = [f for f in fs if still_gone(f, a.repo, a.head)]
            if not gone:
                continue
            spans = [s for s in reach.candidates(root, gone, exports=exports, modules=modules) if not reach.is_test_path(s.file)]
            det, ask = [], []
            for s in spans:
                if "Public object was removed" not in s.finding.message:
                    # a parameter change: today's call against today's signature, nothing else
                    if s.ambiguous and s.receiver != "self":
                        continue          # an unknown receiver the tree also defines: its type decides, so a person does
                    if is_override(s.code):
                        continue          # an override's signature, not a call against today's signature
                    fn = positional._def_at(a.repo, a.head, s.finding.path, s.finding.object_name)
                    parts = s.finding.object_name.split(".")
                    names = {parts[-1] if parts[-1] != "__init__" else parts[-2], s.alias} - {""}
                    ok = binds(s.code, fn, names, method=len(parts) > 1) if fn is not None else None
                    if ok is False:
                        det.append((s, rules.RULE_BREAK))
                    continue
                v = rules.decide(s, a.repo, sha + "^", a.head)
                (det if v else ask).append((s, v))
            b = budget.Budget(run_usd=a.budget_usd)
            states = [evidence.render(s, a.package, sha + "^", a.head) for s, _ in ask]
            verdicts = verify.cap_ambiguous([s for s, _ in ask], verify.verify(states, budget=b)) if states else []
            b.close(label=f"head {a.package} {sha[:8]}")
            for s, v, how in [(s, v, "rule") for s, v in det] + [(s, v, "model") for (s, _), v in zip(ask, verdicts)]:
                if v.status != "breaks_consumer" or (s.file, s.line, s.finding.describe()) in seen:
                    continue
                seen.add((s.file, s.line, s.finding.describe()))
                src = (pathlib.Path(root) / s.file).read_text(errors="replace")
                run = fixcheck.runs(src, s.file, root, a.repo, a.package, head_sha, a.search, a.python) if a.python else None
                if run is not None and run.status == "fail":
                    name = s.finding.object_name.split(".")[-1] if s.finding.object_name != "<module>" else s.finding.module
                    run.status = "reproduced" if name in run.detail else "fails for another reason"
                rec = {"commit": sha, "date": date, "subject": subj[:90], "file": s.file, "line": s.line, "code": s.code.strip()[:160],
                       "finding": s.finding.describe(), "how": how, "reach": round(v.reach, 2), "conf": round(v.break_confidence, 2),
                       "import_at_head": None if run is None else f"{run.status}: {run.detail}"[:200]}
                out.write(json.dumps(rec) + "\n"); out.flush(); hits += 1
                print(f"  HIT {s.file}:{s.line}  {s.finding.describe()[:80]}  (removed by {sha[:8]} {date})  import: {rec['import_at_head']}", flush=True)
    print(f"{hits} reference(s) at {a.head} to something an earlier commit removed or changed, still gone today")


if __name__ == "__main__":
    main()
