#!/usr/bin/env python3
"""Run history_bench over every Python package of a repository (any layout) and append to one results file.

Packages: `src/<pkg>`, `<x>/src/<pkg>`, `<pkg>/`, `<dir>/<pkg>/` — any directory holding an __init__.py at
the ref, tests excluded. Usage: history_bench_all.py <repo> [--ref origin/main] [--since 2020-01-01]
[--max-commits 60] [--consumer-sub DIR] [--budget-usd 0.01] [--out results.jsonl]"""
from __future__ import annotations

import argparse, pathlib, re, subprocess, sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo"); ap.add_argument("--ref", default="origin/main"); ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--max-commits", type=int, default=60); ap.add_argument("--consumer-sub", default="")
    ap.add_argument("--budget-usd", type=float, default=0.01); ap.add_argument("--out", default="spike/history-bench-all.jsonl")
    ap.add_argument("--skip", default=r"(^|/)(tests?|docs|docs_src|examples|scripts)/",
                    help="regex of directories that are not library packages (docs_src/* examples were each benchmarked as a package)")
    a = ap.parse_args()
    files = subprocess.run(["git", "-C", a.repo, "ls-tree", "-r", "--name-only", a.ref], capture_output=True, text=True).stdout.splitlines()
    pkgs = set()
    for f in files:
        if not f.endswith("/__init__.py") or re.search(a.skip, f):
            continue
        d = f[: -len("/__init__.py")]
        m = re.match(r"^((?:.*/)?src)/([A-Za-z_]\w*)$", d)
        if m:
            pkgs.add((m.group(1), m.group(2))); continue
        parts = d.split("/")
        if len(parts) <= 2 and parts[-1].isidentifier() and not any(p.startswith(".") for p in parts):
            pkgs.add(("/".join(parts[:-1]), parts[-1]))
    # a nested package inside one already listed is covered by its parent
    pkgs = sorted(p for p in pkgs if not any(q != p and (f"{q[0]}/{q[1]}" if q[0] else q[1]) == p[0] for q in pkgs))
    print(f"{a.repo}: {len(pkgs)} packages -> {[f'{s}/{p}' if s else p for s, p in pkgs]}")
    open(a.out, "w").close()                    # start afresh; each package then appends
    for search, pkg in pkgs:
        cmd = [sys.executable, str(pathlib.Path(__file__).with_name("history_bench.py")), a.repo, pkg, "--ref", a.ref,
               "--since", a.since, "--max-commits", str(a.max_commits), "--budget-usd", str(a.budget_usd), "--out", a.out, "--append"]
        if search:
            cmd += ["--search", search]
        if a.consumer_sub:
            cmd += ["--consumer-sub", a.consumer_sub]
        r = subprocess.run(cmd, capture_output=True, text=True)
        tail = [l for l in r.stdout.splitlines() if l.startswith("pooled") or "first-parent commits" in l]
        print(f"  {search + '/' if search else ''}{pkg}: {' | '.join(tail)[:200]}" + (f"  ERR {r.stderr.strip().splitlines()[-1][:120]}" if r.returncode else ""))


if __name__ == "__main__":
    main()
