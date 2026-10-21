#!/usr/bin/env python3
"""Route-contract history across a repository boundary: for every first-parent commit of a service that removed
routes (estate-wide: no service exposed them afterwards), the consumer at that commit's date is checked for
lines naming those routes; each such line is then followed forward — did a later consumer commit rewrite it
(a fix), or is it still there (a live or dead call)?
Usage: route_history.py --lib <service repo> --ref origin/main --since 2026-04-01 --consumer <ui repo>
       --consumer-ref origin/main --estate <repo>=<ref> ..."""
from __future__ import annotations

import argparse, datetime, pathlib, subprocess, tempfile
from aftershock import routes


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True); ap.add_argument("--ref", default="origin/main"); ap.add_argument("--since", default="2026-04-01")
    ap.add_argument("--consumer", required=True); ap.add_argument("--consumer-ref", default="origin/main")
    ap.add_argument("--estate", action="append", default=[]); ap.add_argument("--ext", default=".ts,.tsx,.js,.jsx")
    a = ap.parse_args()
    surviving_head = routes.estate_routes([tuple(e.split("=", 1)) for e in a.estate])   # mounts resolved across repositories
    # only commits whose diff touches a route decorator or a router mount can change the route table
    commits = git(a.lib, "log", "--first-parent", "--format=%H %ad", "--date=short", f"--since={a.since}",
                  "-G" + r"@\w+\.(get|post|put|patch|delete|head|options|websocket|api_route|route)\(|include_router\(|APIRouter\(", a.ref).splitlines()
    print(f"{len(commits)} first-parent commits since {a.since} touch a route decorator or mount; estate routes at HEAD: {len(surviving_head)}")
    n_removed = n_named = n_fixed = n_open = 0
    for line in reversed(commits):
        sha, date = line.split()
        old = routes.routes(a.lib, sha + "^1"); new = routes.routes(a.lib, sha)
        gone = routes.diff(old, new, surviving_head)["removed"]      # never exposed again anywhere in the estate
        if not gone:
            continue
        n_removed += len(gone)
        csha = git(a.consumer, "rev-list", "-1", f"--before={date}T23:59:59", a.consumer_ref).strip()
        if not csha:
            continue
        with tempfile.TemporaryDirectory(prefix="aftershock-ui-") as tmp:
            subprocess.run(f"git -C {a.consumer} archive {csha} | tar -x -C {tmp}", shell=True, check=False)
            hits = routes.consumers(tmp, [r.path for r in gone], tuple(a.ext.split(",")))
        named = [(r, f, ln, code) for r in gone for f, ln, code in hits.get(r.path, [])]
        print(f"\n{sha[:8]} {date} {git(a.lib, 'log', '-1', '--format=%s', sha).strip()[:60]}: {len(gone)} routes gone, consumer names {len(named)} lines")
        for r, f, ln, code in named:
            n_named += 1
            later = git(a.consumer, "log", "--first-parent", "--format=%h|%ad|%s", "--date=short", f"-S{code}", f"{csha}..{a.consumer_ref}", "--", f).strip().splitlines()
            if later:
                fsha, fdate, subj = later[-1].split("|", 2)
                days = (datetime.date.fromisoformat(fdate) - datetime.date.fromisoformat(date)).days
                n_fixed += 1; status = f"rewritten by {fsha} +{days}d {subj[:45]}"
            else:
                n_open += 1; status = "STILL PRESENT at consumer HEAD"
            print(f"   {r.method.upper():7s} {r.path:45s} {f}:{ln}  {status}")
    print(f"\nroutes gone from the estate: {n_removed}; consumer lines naming one at the time: {n_named}; later rewritten: {n_fixed}; still present: {n_open}")


if __name__ == "__main__":
    main()
