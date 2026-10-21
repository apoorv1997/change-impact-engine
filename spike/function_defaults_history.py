#!/usr/bin/env python3
"""The sibling class: function-parameter defaults and class-attribute values that changed (griffe: "Parameter default
was changed", "Attribute value was changed"), which the history benchmark skips by design, followed forward for a
later fix-type commit that touched the same object. Usage: function_defaults_history.py --repo <path> --ref <ref> --since <date>"""
from __future__ import annotations

import argparse, datetime, re, subprocess
from aftershock import contracts
from aftershock.prcheck import packages


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--repo", required=True); ap.add_argument("--ref", default="origin/main"); ap.add_argument("--since", default="2026-04-01")
    a = ap.parse_args()
    commits = git(a.repo, "log", "--first-parent", "--format=%H %ad %s", "--date=short", f"--since={a.since}", a.ref, "--", "*.py").splitlines()
    print(f"{a.repo.split('/')[-1]}: {len(commits)} first-parent commits since {a.since} touch Python")
    n_changes = 0
    for line in commits:
        sha, date, subj = line.split(" ", 2)
        size = sum(int(w[0]) for part in git(a.repo, "diff", "--shortstat", sha + "^1", sha).split(",") for w in [part.strip().split()] if len(w) >= 2 and w[1].startswith(("insertion", "deletion")))
        if size > 3000:
            continue
        for search, pkg in packages(a.repo, sha + "^1", sha):
            fs = [f for f in contracts.griffe_findings(pkg, sha + "^1", sha, cwd=a.repo, search=search)
                  if "default was changed" in f.message or "Attribute value was changed" in f.message]
            for f in fs:
                n_changes += 1
                name = f.object_name.split(".")[-1] if f.object_name.split(".")[-1] != "__init__" else f.object_name.split(".")[-2]
                later = git(a.repo, "log", "--first-parent", "--format=%h|%ad|%s", "--date=short", "-S" + name, f"{sha}..{a.ref}", "--", "*.py").strip().splitlines()
                fixes = [l for l in later if re.search(r"\bfix|revert|hotfix", l.split("|", 2)[2], re.I)]
                if not fixes:
                    continue
                h, d, s_ = fixes[-1].split("|", 2)
                days = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(date)).days
                if days > 90:
                    continue
                print(f"  {date} {sha[:8]} {f.describe()[:95]:<96} → FIX {h} +{days}d {s_[:50]}   [{subj[:35]}]")
    print(f"{n_changes} default/attribute-value changes examined")


if __name__ == "__main__":
    main()
