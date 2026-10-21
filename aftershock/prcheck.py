"""One PR, every applicable lens, one comment. Driven by an estate manifest (JSON):

  repos:          name -> {slug, path, default, ext?}
  libraries:      [{repo, package, search, consumers, pins?: [{repo, file, pattern}]}]
  named_queries:  {registry_repo, registry_path, consumers}
  routes:         {services, consumers}
  charts:         {repo, apps: {app: {code_repo, code_sub, chart}}}

Every touched Python package is checked against the repository at the PR head (callers the PR forgot) and,
when the manifest lists it as a library, against each consumer's default branch. Nothing is posted unless
--post is given; the comment carries a marker so a later run edits it instead of adding another."""
from __future__ import annotations

import collections, json, os, pathlib, re, subprocess, sys, tempfile

MARK = "<!-- aftershock:pr-check -->"
# a settings module: config/settings in the file name OR any directory of the path (Prefect: settings/models/root.py)
SETTINGS_PATH = re.compile(r"(^|/)(\w*(config|settings)\w*\.py|\w*(config|settings)\w*/.+\.py)$")


def sh(*a, cwd=None, check=False):
    r = subprocess.run(a, capture_output=True, text=True, cwd=cwd)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout


def git(repo, *a):
    return sh("git", "-C", repo, *a)


def packages(repo, base, head):
    """(search, package) for every Python package the PR touched (tests excluded), any layout: a changed .py/.pyi
    belongs to the TOP-most chain of directories that each hold an __init__ — `src/click`, `libs/mylib/src/mylib`,
    `pydantic-core/python/pydantic_core` (pydantic #12924's stub change was missed by a two-levels-deep scan)."""
    tree = set(git(repo, "ls-tree", "-r", "--name-only", head).splitlines()) | \
           set(git(repo, "ls-tree", "-r", "--name-only", base).splitlines())       # a deleted module's package too
    has_init = lambda p: any("/".join(p + [n]) in tree for n in ("__init__.py", "__init__.pyi"))
    out = set()
    for f in git(repo, "diff", "--name-only", base, head).splitlines():
        if not f.endswith((".py", ".pyi")) or re.search(r"(^|/)tests?/", f):
            continue
        parts = f.split("/")[:-1]
        if not parts or not has_init(parts):
            continue                                   # a script or a namespace directory: no package to diff
        i = len(parts)
        while i > 1 and has_init(parts[:i - 1]) and parts[i - 2].isidentifier():
            i -= 1
        if parts[i - 1].isidentifier():
            out.add(("/".join(parts[:i - 1]), parts[i - 1]))
    return sorted(out)


def archive(repo, ref, sub=""):
    tmp = tempfile.mkdtemp(prefix="aftershock-pr-")
    subprocess.run(f"git -C {repo} archive {ref} {sub or '.'} | tar -x -C {tmp}", shell=True, check=False)
    return tmp


class CheckFailed(RuntimeError):
    """A lens could not run. Never reported as clean: an empty result from a crashed check looks like 'no caller affected'."""


def api_impact(aftershock, lib, pkg, search, old, new, consumer, budget):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out = f.name
    r = subprocess.run([aftershock, "api-impact", "--lib", lib, "--package", pkg, "--search", search, "--old", old, "--new", new,
                        "--consumer", consumer, "--no-tests", "--budget-usd", str(budget), "--json", out],
                       capture_output=True, text=True)
    text = r.stdout
    try:
        # a successful run always writes the JSON (an empty list when nothing is affected)
        if r.returncode or not os.path.getsize(out):
            tail = (r.stderr.strip().splitlines() or ["no output"])[-1][:160]
            raise CheckFailed(f"`aftershock api-impact` exited {r.returncode}: {tail}")
        m = re.search(r"Contract diff .*?: (\d+) findings", text)
        return (int(m.group(1)) if m else 0), json.load(open(out)), text
    except (OSError, ValueError) as e:
        raise CheckFailed(f"`aftershock api-impact` left no readable result ({e.__class__.__name__})")
    finally:
        if os.path.exists(out):
            os.unlink(out)


def run(a):
    est = json.load(open(os.path.expanduser(a.estate)))
    repos = {k: {**v, "path": os.path.expanduser(v["path"])} for k, v in est["repos"].items()}
    me = repos[a.repo]; R = me["path"]; head = f"refs/pr/{a.pr}"
    git(R, "fetch", "-q", "origin", f"refs/pull/{a.pr}/head:{head}", me["default"])
    base = git(R, "merge-base", f"origin/{me['default']}", head).strip()
    title = sh("gh", "pr", "view", str(a.pr), "-R", me["slug"], "--json", "title", "-q", ".title").strip()
    files = git(R, "diff", "--name-only", base, head).splitlines()
    behind = git(R, "rev-list", "--count", f"{head}..origin/{me['default']}").strip()
    aftershock = a.aftershock
    from . import contracts
    checks: list[tuple[str, str, str]] = []     # (what was checked, against, result)
    breaks: list[str] = []; looks: list[str] = []; notes: list[str] = []; failed: list[str] = []
    tmpdirs = []
    own_root = archive(R, head); tmpdirs.append(own_root)
    covered: set[str] = set()
    # 1. public-API impact per touched package
    for search, pkg in packages(R, base, head):
        covered |= {f for f in files if f.startswith((search + "/" if search else "") + pkg + "/")}
        findings = contracts.griffe_findings(pkg, base, head, cwd=R, search=search)
        what = f"public API of `{pkg}`"
        if not findings:
            checks.append((what, "—", "no public-API change"))
            continue
        def short(d: str) -> str:                       # `pkg.mod.Obj.attr: Attribute value was changed: Field(...) -> Field(...)` -> `Obj.attr: default changed`
            head, _, msg = d.partition(": ")
            head = ".".join(head.split(".")[-2:]) if head.count(".") > 1 else head
            msg = re.sub(r"Attribute value was changed:.*", "default changed", msg)
            msg = re.sub(r"Parameter default was changed:.*", "default changed", msg)
            return f"{head}: {msg}"[:90]
        desc = [short(f.describe()) for f in findings]
        what += f" — {len(desc)} change{'s' if len(desc) > 1 else ''}: " + "; ".join(f"`{d}`" for d in desc[:3]) + (f" … +{len(desc) - 3} more" if len(desc) > 3 else "")
        consumers = [("this repository at the PR head", own_root)]
        for lib in est.get("libraries", []):
            if lib["repo"] == a.repo and lib["package"] == pkg:
                for c in lib["consumers"]:
                    if c != a.repo:
                        git(repos[c]["path"], "fetch", "-q", "origin", repos[c]["default"])
                        d = archive(repos[c]["path"], f"origin/{repos[c]['default']}"); tmpdirs.append(d)
                        consumers.append((f"{c} @ {repos[c]['default']}", d))
                for pin in lib.get("pins", []):
                    src = git(repos[pin["repo"]]["path"], "show", f"origin/{repos[pin['repo']]['default']}:{pin['file']}")
                    m = re.search(pin["pattern"], src)
                    if m:
                        n = git(R, "rev-list", "--count", f"{m.group(1)}..{head}", "--", lib["search"]).strip()
                        notes.append(f"{pin['repo']} pins `{pkg}` at `{m.group(1)[:8]}` in `{pin['file']}` — {n} `{pkg}` commit{'s' if n != '1' else ''} behind this PR; it will not see this change until the pin moves")
        for name, croot in consumers:
            try:
                _, rows, _ = api_impact(aftershock, R, pkg, search, base, head, croot, a.budget_usd)
            except CheckFailed as e:
                failed.append(f"{what.split(' — ')[0]} against {name}: {e}")
                checks.append((what, name, f"⚠️ **check did not run** — {e}")); what = f"same, `{pkg}`"
                continue
            ver = [r for r in rows if r["status"] == "breaks_consumer"]; rev = [r for r in rows if r["status"] == "needs_review"]
            ok = sum(1 for r in rows if r["status"] == "compatible")
            if ver:
                res = f"❌ **{len(ver)} place{'s' if len(ver) > 1 else ''} break**" + (f", {len(rev)} to look at" if rev else "")
            elif rev:
                res = f"❓ {len(rev)} place{'s' if len(rev) > 1 else ''} to look at"
            else:
                res = f"✅ no caller affected" + (f" ({ok} use{'s' if ok != 1 else ''} still valid)" if ok else "")
            checks.append((what, name, res)); what = f"same, `{pkg}`"
            for r in ver[:a.max_lines]:
                breaks.append(f"`{r['file']}:{r['line']}` — {r['finding']}" + ("" if name.startswith("this") else f"  _(in {name.split(' @')[0]})_"))
            for r in rev[:a.max_lines]:
                looks.append(f"`{r['file']}:{r['line']}` — {r['finding']} (reach {r['reach']:.2f}, break {r['confidence']:.2f})" + ("" if name.startswith("this") else f"  _(in {name.split(' @')[0]})_"))
    # 2. named-query registry contract
    nq = est.get("named_queries")
    if nq and nq["registry_repo"] == a.repo and any(f.startswith(nq["registry_path"]) for f in files):
        covered |= {f for f in files if f.startswith(nq["registry_path"])}
        for c in nq["consumers"]:
            git(repos[c]["path"], "fetch", "-q", "origin", repos[c]["default"])
            d = archive(repos[c]["path"], f"origin/{repos[c]['default']}"); tmpdirs.append(d)
            text = sh(aftershock, "named-queries", "--lib", R, "--src", nq["registry_path"], "--old", base, "--new", head, "--consumer", d, "--no-tests")
            hdr = next((l for l in text.splitlines() if l.startswith("Named-query contract")), "")
            m = re.search(r": (\d+) -> (\d+) queries, (\d+) changes", hdr)
            detail = [l.strip() for l in text.split("\nConsumer ")[0].splitlines()[1:] if l.strip() and "signature unchanged" not in l]
            changes = [l for l in detail if re.match(r"(removed|required_added|optional_added|renamed|changed)", l)]
            res = "✅ no query signature changed" if m and m.group(3) == "0" else ("⚠️ " + "; ".join(changes)[:220] if changes else "changed")
            sites = [l for l in detail if l not in changes]
            checks.append((f"named-query signatures the data adapter serves ({m.group(1) if m else '?'} queries)", f"{c} call sites", res + ("" if not sites else " — " + "; ".join(sites)[:300])))
    # 3. HTTP route contract vs UI consumers
    rt = est.get("routes")
    if rt and a.repo in rt["services"] and any(re.search(r"\.py$", f) for f in files):
        estate = [f"{repos[s]['path']}=origin/{repos[s]['default']}" for s in rt["services"]]
        for c in rt["consumers"]:
            git(repos[c]["path"], "fetch", "-q", "origin", repos[c]["default"])
            d = archive(repos[c]["path"], f"origin/{repos[c]['default']}"); tmpdirs.append(d)
            text = sh(a.python, str(pathlib.Path(__file__).parent.parent / "spike" / "routes_estate.py"), "--lib", R, "--old", base, "--new", head,
                      *[x for e in estate for x in ("--estate", e)], "--consumer", d, "--ext", repos[c].get("ext", ".ts,.tsx,.js,.jsx"))
            gone = re.search(r"gone from the estate: (\d+)", text); named = re.search(r"consumer .*: (\d+) lines", text)
            hits = [l.strip() for l in text.splitlines() if l.startswith("  ") and "|" in l][:a.max_lines]
            if gone and int(gone.group(1)):
                checks.append(("HTTP routes this service exposes", f"URL literals in {c}", f"{gone.group(1)} route{'s' if gone.group(1) != '1' else ''} removed from the whole estate; " + (f"❌ {c} still names {named.group(1)}" if named and int(named.group(1)) else "✅ none used by the UI")))
                breaks.extend(f"{h}  _(UI still calls a removed route)_" for h in hits)
            else:
                checks.append(("HTTP routes this service exposes", f"URL literals in {c}", "✅ no route removed"))
    # 4. env-key contract vs the deployed chart
    ch = est.get("charts")
    if ch:
        hrepo = repos[ch["repo"]]; git(hrepo["path"], "fetch", "-q", "origin", hrepo["default"])
        hroot = archive(hrepo["path"], f"origin/{hrepo['default']}", ch.get("root", "charts")); tmpdirs.append(hroot)   # "root": where the charts live
        for app, spec in ch["apps"].items():
            touched_code = spec["code_repo"] == a.repo and any(f.startswith(spec["code_sub"]) for f in files) and any(SETTINGS_PATH.search(f) for f in files)
            touched_chart = ch["repo"] == a.repo and any(f.startswith(spec["chart"]) for f in files)
            if not (touched_code or touched_chart):
                continue
            covered |= {f for f in files if f.startswith(spec["chart"])}
            code = os.path.join(own_root if spec["code_repo"] == a.repo else archive(repos[spec["code_repo"]]["path"], f"origin/{repos[spec['code_repo']]['default']}"), spec["code_sub"])
            chart = os.path.join(own_root if touched_chart else hroot, spec["chart"])
            values = spec.get("values", ["values.yaml"])          # a chart's values files; an environment overlay can be listed too
            text = sh(aftershock, "config-contract", "--code", code, "--chart", *[f"{chart}/{v}" for v in values], f"{chart}/templates")
            req = re.search(r"REQUIRED by code \(no default\), not set by chart \((\d+)\): ?(.*)", text)
            unread = re.search(r"set by chart, read nowhere \((\d+)\)", text)
            nreq = int(req.group(1)) if req else 0
            res = (f"❌ {nreq} required key{'s' if nreq > 1 else ''} the chart never sets: `{req.group(2).strip()}`" if nreq else "✅ every required key is set") + (f"; {unread.group(1)} chart key{'s' if unread.group(1) != '1' else ''} nothing reads" if unread and unread.group(1) != "0" else "")
            checks.append((f"env keys `{app}` reads (settings + os.getenv)", f"its chart `{spec['chart']}`", res))
    # 5. settings defaults: a new env-backed setting, or a changed default, changes behaviour for every deployment
    #    that does not set the key (a new timeout with a default cuts short every job that runs longer)
    from .config_contract import collect_helpers, settings_defaults
    helpers = collect_helpers(own_root)                         # env_prefix built by a helper function (public estates)
    renamed = {}                                                # new path -> old path when the PR moved the file
    for l in git(R, "diff", "-M", "--name-status", base, head).splitlines():
        parts = l.split("\t")
        if parts[0].startswith("R") and len(parts) == 3:
            renamed[parts[2]] = parts[1]
    for f in files:
        if not f.endswith(".py") or not SETTINGS_PATH.search(f) or re.search(r"(^|/)tests?/", f):
            continue
        old_src = git(R, "show", f"{base}:{renamed.get(f, f)}"); new_src = git(R, "show", f"{head}:{f}")
        if "BaseSettings" not in new_src and "BaseSettings" not in old_src:
            continue
        od, nd = settings_defaults(old_src, helpers), settings_defaults(new_src, helpers)
        changed = [(k, od.get(k), nd[k]) for k in nd if k not in od or od[k] != nd[k]]
        if not changed:
            continue
        covered.add(f)
        # which chart apps of this code repo set these keys (stage + common values + templates)
        setters: dict[str, set] = {k: set() for k, _, _ in changed}
        chart_value: dict[str, str] = {}
        if ch:
            for app, spec in ch["apps"].items():
                if spec["code_repo"] != a.repo or not f.startswith(spec["code_sub"]):
                    continue
                chart = os.path.join(hroot, spec["chart"])
                txt = ""
                for cf in [f"{chart}/{v}" for v in spec.get("values", ["values.yaml"])]:
                    txt += open(cf).read() if os.path.exists(cf) else ""
                for tf in pathlib.Path(chart, "templates").glob("*.y*ml") if os.path.isdir(os.path.join(chart, "templates")) else []:
                    txt += tf.read_text(errors="replace")
                for k, _, _ in changed:
                    m = re.search(rf"^\s*{re.escape(k)}\s*:\s*(.+?)\s*$", txt, re.M)
                    if m or re.search(rf"\b{re.escape(k)}\b", txt):
                        setters[k].add(app)
                    if m:
                        chart_value[k] = m.group(1).strip().strip("'\"")
        changed.sort(key=lambda t: (bool(setters[t[0]]), t[1] is not None))   # unset new keys first, then changed defaults
        items = []
        def same_value(a_: str, b_: str) -> bool:
            return a_.strip("'\"") == b_.strip("'\"")
        for k, o, n in changed[:a.max_lines]:
            kind = "new" if o is None else "default changed"
            cv = chart_value.get(k)
            if not setters[k]:
                where = "**no chart sets it → the default applies to every deployment**"
            elif o is not None and cv is not None and same_value(cv, o) and not same_value(cv, n):
                where = f"**chart {', '.join(sorted(setters[k]))} pins the OLD value `{cv}` → the new default never reaches that deployment**"
            elif cv is not None and not same_value(cv, n):
                where = f"chart {', '.join(sorted(setters[k]))} sets `{cv}` (differs from the default)"
            else:
                where = f"set by chart {', '.join(sorted(setters[k]))}"
            items.append(f"`{k}` {kind}: {(o + ' → ') if o else ''}`{n}` — {where}")
        more = f" … +{len(changed) - a.max_lines} more" if len(changed) > a.max_lines else ""
        checks.append((f"settings defaults in `{f.split('/')[-1]}`" + (" (file moved)" if f in renamed else ""), "the deployed charts", "⚠️ " + "; ".join(items) + more))
    # what the PR touched that no lens reads
    is_test = lambda f: bool(re.search(r"(^|/)tests?/|(^|/)test_[^/]+\.py$|conftest\.py$", f))
    uncovered = collections.Counter("/".join(f.split("/")[:2]) for f in files if f not in covered and not is_test(f))
    uncovered_py = collections.Counter()
    # compose
    n_break = len(breaks); n_look = len(looks)
    verdict = (f"❌ **{n_break} place{'s' if n_break > 1 else ''} in the estate break{'' if n_break > 1 else 's'} because of this PR**" + (f", {n_look} more to look at" if n_look else "")
               if n_break else (f"❓ **nothing verified as breaking; {n_look} place{'s' if n_look > 1 else ''} for a person to look at**" if n_look else "✅ **nothing in the estate breaks because of this PR** (within what was checked — see below)"))
    if failed:                                               # a check that did not run is never part of a clean verdict
        unrun = f"⚠️ **{len(failed)} check{'s' if len(failed) > 1 else ''} did not run — not a clean result**"
        verdict = f"{verdict}; {unrun}" if (n_break or n_look) else unrun
    lines = [MARK, f"## Change impact — #{a.pr} `{title[:70]}`", "", verdict, ""]
    if checks:
        lines += ["| What was checked | Against | Result |", "|---|---|---|"]
        lines += [f"| {w} | {ag} | {r} |" for w, ag, r in checks]
        lines.append("")
    if breaks:
        lines += [f"### ❌ Breaks ({n_break}) — change these before merging", *[f"- {b}" for b in breaks], ""]
    if looks:
        lines += [f"### ❓ Look at ({n_look}) — evidence exists, a person decides", *[f"- {l}" for l in looks], ""]
    for n in notes:
        lines.append(f"**Pin:** {n}"); lines.append("")
    if uncovered or uncovered_py:
        parts = [f"`{d}` ({n} file{'s' if n > 1 else ''})" for d, n in uncovered.most_common(6)]
        lines.append(f"**Not covered:** {', '.join(parts)} — scripts, configuration, YAML, and edits that change behaviour without changing a signature are outside what this check can see. Tests are skipped on purpose."); lines.append("")
    lines += ["<details><summary>How to read this</summary>", "",
              f"- Compared against `{me['default']}` at merge-base `{base[:8]}`; this branch is {behind} commit{'s' if behind != '1' else ''} behind it.",
              "- ❌ = that line stops working when this PR merges (a removed object, a parameter it no longer accepts, an argument now landing in the wrong place). ❓ = the evidence is real but the receiver's type or the intent is unclear; a person decides.",
              "- \"no caller affected\" = every use of the changed API in the checked code still works, or no such use exists; \"still valid\" counts the uses that were checked.",
              "- Decided by code wherever code can decide; a calibrated model judges only the rest, under a spend cap. Nothing here is a risk score."]
    if est.get("basis"):
        lines.append(f"- Basis: {est['basis']}.")
    lines += ["", "</details>"]
    body = "\n".join(lines)
    for d in tmpdirs:
        subprocess.run(["rm", "-rf", d])
    print(body)
    word = 'break' if n_break else ('unchecked' if failed else ('look' if n_look else 'clean'))
    print(f"\n[verdict] {word} breaks={n_break} looks={n_look} checks={len(checks)} failed={len(failed)}")
    for f_ in failed:
        print(f"[check failed] {f_}", file=sys.stderr)
    existing = sh("gh", "api", f"repos/{me['slug']}/issues/{a.pr}/comments", "--jq", f'.[] | select(.body | startswith("{MARK}")) | .id').strip().splitlines() if (a.post or a.only_breaks) else []
    if a.only_breaks and not n_break and failed:             # unchecked is not clean: keep any earlier comment, fail the run
        print("[kept earlier comment, if any: a check did not run]")
        sys.exit(2)
    if a.only_breaks and not n_break:                        # policy: a comment only where something breaks
        for cid in existing:
            sh("gh", "api", "-X", "DELETE", f"repos/{me['slug']}/issues/comments/{cid}")
            print(f"[removed earlier comment {cid}: nothing breaks]")
        return
    if a.post and not checks:
        print("\n[not posted: nothing to say for this PR]")
        return
    if a.post or a.only_breaks:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body); path = f.name
        if existing:
            sh("gh", "api", "-X", "PATCH", f"repos/{me['slug']}/issues/comments/{existing[0]}", "-F", f"body=@{path}", check=True)
            print(f"\n[updated comment {existing[0]}]")
        else:
            sh("gh", "pr", "comment", str(a.pr), "-R", me["slug"], "--body-file", path, check=True)
            print("\n[posted]")
        os.unlink(path)
