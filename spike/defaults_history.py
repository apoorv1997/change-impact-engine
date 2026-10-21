#!/usr/bin/env python3
"""Behaviour changes hidden in defaults, through history: every first-parent commit that added an env-backed
setting with a default or changed one (pydantic Settings, rename-aware), followed forward for a later commit in
the same repository that touched that key's default again, and for the chart repository's first commit naming
the key after that date. A later "fix" naming the key is the signal that the default hurt someone.
Usage: defaults_history.py --repo <path> --ref origin/main --since 2026-04-01 [--chart <helm repo>=<ref>]"""
from __future__ import annotations

import argparse, datetime, re, subprocess
from aftershock.config_contract import settings_defaults


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--repo", required=True); ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--since", default="2026-04-01"); ap.add_argument("--chart"); a = ap.parse_args()
    chart_repo, chart_ref = (a.chart.split("=", 1) if a.chart else (None, None))
    commits = git(a.repo, "log", "--first-parent", "--format=%H %ad %s", "--date=short", f"--since={a.since}", a.ref, "--",
                  "*config*.py", "*settings*.py").splitlines()
    print(f"{a.repo.split('/')[-1]}: {len(commits)} first-parent commits since {a.since} touch a config/settings module")
    events = []
    for line in commits:
        sha, date, subj = line.split(" ", 2)
        renamed = {}
        for l in git(a.repo, "diff", "-M", "--name-status", sha + "^1", sha).splitlines():
            p = l.split("\t")
            if p[0].startswith("R") and len(p) == 3:
                renamed[p[2]] = p[1]
        for f in git(a.repo, "diff", "--name-only", sha + "^1", sha).splitlines():
            if not re.search(r"(config|settings)\w*\.py$", f) or re.search(r"(^|/)tests?/", f):
                continue
            old = settings_defaults(git(a.repo, "show", f"{sha}^1:{renamed.get(f, f)}")); new = settings_defaults(git(a.repo, "show", f"{sha}:{f}"))
            for k in new:
                if k not in old or old[k] != new[k]:
                    events.append((date, sha, subj, f, k, old.get(k), new[k]))
    print(f"{len(events)} new keys / changed defaults\n")
    print(f"{'date':<11}{'commit':<9}{'key':<44}{'default':<22}{'later in code':<34}{'later in chart':<34} subject")
    for date, sha, subj, f, k, o, n in events:
        later_code = git(a.repo, "log", "--first-parent", "--format=%h|%ad|%s", "--date=short", "-S" + k, f"{sha}..{a.ref}", "--", "*.py").strip().splitlines()
        later_chart = git(chart_repo, "log", "--first-parent", "--format=%h|%ad|%s", "--date=short", f"--since={date}", "-S" + k, chart_ref).strip().splitlines() if chart_repo else []
        def fmt(lst):
            if not lst:
                return "—"
            h, d, s_ = lst[-1].split("|", 2)
            days = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(date)).days
            flag = "FIX " if re.search(r"\bfix|revert|hotfix", s_, re.I) else ""
            return f"{flag}{h} +{days}d {s_[:18]}"
        kind = "new" if o is None else f"{o[:8]}→"
        print(f"{date:<11}{sha[:8]:<9}{k[:43]:<44}{(kind + n)[:21]:<22}{fmt(later_code)[:33]:<34}{fmt(later_chart)[:33]:<34} {subj[:40]}")


if __name__ == "__main__":
    main()
