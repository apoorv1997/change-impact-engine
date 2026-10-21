#!/usr/bin/env python3
"""Benchmark aftershock against a repository's own history.

For every commit that changed a library package's public API (griffe findings of the kinds aftershock
handles), the consumer is the same repo at the parent commit and the ground truth is the commit's
own diff: a candidate line the commit changed or deleted had to change (break); an untouched line
did not. Outputs precision/recall per commit and pooled, plus the model's reach/break answers with
their truth for recalibration. Takes repo paths only; the results file may contain private paths.

Usage: history_bench.py <lib-repo> <package> [--search SRC] [--consumer-sub DIR] [--since DATE]
                        [--max-commits N] [--budget-usd X] [--out results.jsonl] [--append]"""
from __future__ import annotations

import argparse, collections, json, pathlib, re, subprocess, sys, tempfile, time
from aftershock import budget, contracts, evidence, positional, reach, rules, verify


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def changed_ranges(repo, old, new, sub):
    """Old-side line ranges per file (relative to `sub`) changed by old..new, plus deleted files."""
    diff = git(repo, "diff", "-U0", old, new, *(["--", sub] if sub else []))   # `-- ""` matches nothing
    ranges, cur, deleted, added = collections.defaultdict(list), None, set(), set()
    for l in diff.splitlines():
        if l.startswith("--- a/"):
            cur = l[6:]; deleted.add(cur)
        elif l.startswith("--- /dev/null"):
            cur = None
        elif l.startswith("+++ b/"):
            added.add(l[6:])
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", l)
        if m and cur:
            a = int(m.group(1)); n = int(m.group(2) or 1); ranges[cur].append((a, a + max(n, 1) - 1))
    return ranges, deleted - added


def touched(ranges, gone, sub, file, line):
    f = f"{sub}/{file}" if sub else file
    return f in gone or any(a <= line <= b for a, b in ranges.get(f, []))


def span_touched(ranges, gone, sub, s):
    """Any line of the statement, not just its first (a multi-line call edited on its second line)."""
    return any(touched(ranges, gone, sub, s.file, l) for l in range(s.line, s.line + s.code.count("\n") + 1))


def binding_lines(root, s):
    """Lines of the import statements in the span's file that bind the referenced name: a removed object is
    often repaired at its import (`from app.utils.middleware import X` -> `from logkit import X`)
    while the use line stays as it was."""
    import ast as _ast
    try:
        tree = _ast.parse((pathlib.Path(root) / s.file).read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    want = {s.alias, s.finding.object_name.split(".")[0], s.finding.object_name.split(".")[-1]} - {""}
    if s.receiver and s.receiver not in ("self", "?"):
        want.add(s.receiver.split(".")[0])
    out = []
    for n in _ast.walk(tree):
        if isinstance(n, (_ast.Import, _ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) in want for a in n.names):
            out.extend(range(n.lineno, getattr(n, "end_lineno", n.lineno) + 1))
    return out


def span_or_binding_touched(ranges, gone, sub, s, root):
    return span_touched(ranges, gone, sub, s) or any(touched(ranges, gone, sub, s.file, l) for l in binding_lines(root, s))


def route(s, lib, old, new):
    """Same deterministic routing as the CLI (aftershock.rules.decide); None = ask the model."""
    return rules.decide(s, lib, old, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lib"); ap.add_argument("package")
    ap.add_argument("--search"); ap.add_argument("--consumer-sub", default="", help="subdir of the repo to scan as consumer ('' = whole repo)")
    ap.add_argument("--since", default="2026-07-01"); ap.add_argument("--max-commits", type=int, default=40)
    ap.add_argument("--budget-usd", type=float, default=0.01); ap.add_argument("--out", default="spike/history-bench.jsonl")
    ap.add_argument("--ref", default="HEAD", help="history to walk")
    ap.add_argument("--griffe-timeout", type=float, default=300, help="skip a commit whose API diff takes longer (seconds)")
    ap.add_argument("--workers", type=int, default=4, help="parallel griffe runs while scanning commits for API changes")
    ap.add_argument("--append", action="store_true", help="add to --out instead of starting it afresh")
    a = ap.parse_args()
    pkg_dir = f"{a.search + '/' if a.search else ''}{a.package}"
    commits = git(a.lib, "log", "--first-parent", "--format=%H %ad %s", "--date=short", f"--since={a.since}", a.ref, "--", pkg_dir).splitlines()
    print(f"{len(commits)} first-parent commits since {a.since} touch {pkg_dir}")
    out = open(a.out, "a" if a.append else "w"); pooled = collections.Counter(); per = []
    done = 0

    def findings_of(line):
        sha = line.split(" ", 1)[0]
        try:
            fs = contracts.griffe_findings(a.package, sha + "^", sha, cwd=a.lib, search=a.search, timeout=a.griffe_timeout)
        except subprocess.TimeoutExpired:
            print(f"  {sha[:8]} skipped: griffe exceeded {a.griffe_timeout:.0f}s", flush=True)
            return []
        return [f for f in fs if any(k in f.message for k in (
            "Parameter was removed", "Public object was removed", "Positional parameter was moved",
            "Parameter was added as required", "Parameter kind was changed"))]

    # griffe on a big package takes minutes per commit and most commits change no API: scan in parallel chunks, in order
    import concurrent.futures as cf
    pool = cf.ThreadPoolExecutor(max_workers=a.workers)
    scanned = {}
    def scan(i):
        chunk = [l for l in commits[i:i + a.workers * 2] if l not in scanned]
        for l, fs in zip(chunk, pool.map(findings_of, chunk)):
            scanned[l] = fs
    for i, line in enumerate(commits):
        if done >= a.max_commits:
            break
        if line not in scanned:
            scan(i)
        sha, date, subj = line.split(" ", 2)
        old = sha + "^"
        findings = scanned.pop(line)
        if not findings:
            continue
        done += 1
        with tempfile.TemporaryDirectory(prefix="aftershock-hist-") as tmp:
            sub = a.consumer_sub
            subprocess.run(f"git -C {a.lib} archive {old} {sub or '.'} | tar -x -C {tmp}", shell=True, check=False)
            root = pathlib.Path(tmp) / sub if sub else pathlib.Path(tmp)
            exports = contracts.package_exports(a.lib, a.package, old, a.search)
            modules = contracts.library_modules(a.lib, a.package, old, a.search)
            spans = [s for s in reach.candidates(root, findings, exports=exports, modules=modules) if not reach.is_test_path(s.file)]
            ranges, gone = changed_ranges(a.lib, old, sha, sub)
            det, ask = [], []
            for s in spans:
                v = route(s, a.lib, old, sha)
                (det if v else ask).append((s, v))
            states = [evidence.render(s, a.package, old, sha) for s, _ in ask]
            b = budget.Budget(run_usd=a.budget_usd)
            verdicts = verify.cap_ambiguous([s for s, _ in ask], verify.verify(states, budget=b)) if states else []
            b.close(label=f"hist {a.package} {sha[:8]}")
            rows = [(s, v, "rule") for s, v in det] + [(s, v, "model") for (s, _), v in zip(ask, verdicts)]
            tp = fp = 0
            for s, v, how in rows:
                truth = span_or_binding_touched(ranges, gone, sub, s, root)
                if v.status == "breaks_consumer":
                    tp += truth; fp += (not truth)
                out.write(json.dumps({"repo": a.lib, "sha": sha, "date": date, "subject": subj[:80], "file": s.file, "line": s.line,
                                      "finding": s.finding.describe()[:120], "how": how, "status": v.status,
                                      "reach": v.reach, "break": v.break_choice, "conf": v.break_confidence,
                                      "truth_touched": truth}) + "\n")
            # recall: references to removed names in the consumer that the commit touched but aftershock did not surface
            removed = [f for f in findings if "Public object was removed" in f.message]
            names = {f.object_name.split(".")[-1] for f in removed}
            members = {f.object_name.split(".")[-1] for f in removed if "." in f.object_name}   # Class.member: only `x.member` counts
            refs = set()
            if names:
                import ast
                for p in root.rglob("*.py"):
                    if reach.is_test_path(str(p.relative_to(root))) or any(x in p.parts for x in (".venv", ".git")):
                        continue
                    if (f"{sub}/{p.relative_to(root)}" if sub else str(p.relative_to(root))) in gone:
                        continue        # a file the commit deleted wholesale leaves no consequence behind
                    try:
                        tree = ast.parse(p.read_text(errors="replace"))
                    except (SyntaxError, ValueError):
                        continue
                    defs = {n.lineno for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names}
                    parents = {c: q for q in ast.walk(tree) for c in ast.iter_child_nodes(q)}
                    bound = {}
                    def shadowed(n, nm):
                        q = parents.get(n)
                        while q is not None:
                            if isinstance(q, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if q not in bound:
                                    bound[q] = reach.bound_names(q)
                                if nm in bound[q]:
                                    return True
                            q = parents.get(q)
                        return False
                    imports_ = reach._import_map(tree, str(p.relative_to(root)))
                    own_mod = reach.module_of(str(p.relative_to(root)))
                    rel_ = str(p.relative_to(root))
                    for n in ast.walk(tree):
                        if isinstance(n, (ast.Import, ast.ImportFrom)):
                            mods = [reach._abs_module(n, own_mod, rel_.endswith("__init__.py"))] if isinstance(n, ast.ImportFrom) else [a.name for a in n.names]
                            if any(f.object_name == "<module>" and (m == f.module or (isinstance(n, ast.ImportFrom) and any(f"{m}.{a.name}" == f.module for a in n.names)))
                                   for f in removed for m in mods) and touched(ranges, gone, sub, rel_, n.lineno):
                                refs.add((rel_, n.lineno))
                            continue
                        nm = n.id if isinstance(n, ast.Name) else (n.attr if isinstance(n, ast.Attribute) else None)
                        if nm in names and isinstance(n, ast.Name) and (nm in members or shadowed(n, nm) or isinstance(n.ctx, ast.Store)):
                            continue        # a bare local / a definition is not a reference to the removed object
                        if nm in names and isinstance(n, ast.Name) and not any(
                                reach.object_matches(n, f, imports_, exports) for f in removed if f.object_name.split(".")[-1] == nm):
                            continue        # imported from a different module (the object moved): not this finding's object
                        if nm in names and n.lineno not in defs:
                            rel = str(p.relative_to(root))
                            if touched(ranges, gone, sub, rel, n.lineno):
                                refs.add((rel, n.lineno))
            got = {(s.file, s.line) for s, _, _ in rows}
            rec_hit = len(refs & got); rec_n = len(refs)
            for rel, ln in sorted(refs - got):          # changed lines naming a removed object that aftershock never surfaced
                out.write(json.dumps({"repo": a.lib, "sha": sha, "date": date, "subject": subj[:80], "file": rel, "line": ln,
                                      "finding": "unsurfaced reference to a removed object", "how": "none", "status": "unsurfaced",
                                      "reach": 0.0, "break": "", "conf": 0.0, "truth_touched": True}) + "\n")
            per.append((sha[:8], date, subj[:50], len(findings), len(spans), tp, fp, rec_hit, rec_n, b.spent_tokens))
            pooled.update(dict(tp=tp, fp=fp, rec_hit=rec_hit, rec_n=rec_n, spans=len(spans), tokens=b.spent_tokens))
            print(f"  {sha[:8]} {date} findings={len(findings):2d} spans={len(spans):3d} verified={tp+fp:3d} "
                  f"(true {tp}, false {fp}) recall={rec_hit}/{rec_n} tokens={b.spent_tokens}  {subj[:48]}")
    out.close()
    p = pooled["tp"] / max(1, pooled["tp"] + pooled["fp"]); r = pooled["rec_hit"] / max(1, pooled["rec_n"])
    print(f"\npooled over {len(per)} commits: precision {pooled['tp']}/{pooled['tp']+pooled['fp']} = {p:.2f}   "
          f"recall {pooled['rec_hit']}/{pooled['rec_n']} = {r:.2f}   spans {pooled['spans']}   "
          f"tokens {pooled['tokens']} (${pooled['tokens']*budget.PRICE_PER_TOKEN:.4f})")


if __name__ == "__main__":
    main()
