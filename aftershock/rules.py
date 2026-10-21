"""Deterministic routing: what code can decide, code decides; the model gets only the rest.

Shared by the CLI and the benchmarks so they can never drift apart."""
from __future__ import annotations

import functools

from . import contracts, positional
from .reach import Span
from .verify import SKIPPED, Verdict

RULE_BREAK = Verdict(1.0, "breaks_consumer", 1.0, {}, 0)
RULE_COMPAT = Verdict(1.0, "compatible", 1.0, {}, 0)
RULE_NOT_REACHED = Verdict(0.0, "compatible", 1.0, {}, 0)


def still_bound(s: Span, lib_root: str, new_ref: str) -> bool:
    """False when the 'removed' name is still bound somewhere in its module at the new ref — a conditional definition
    (`if TYPE_CHECKING: X = ...` / `else: class X`) griffe saw only one branch of. Such a name may or may not be
    importable at runtime, so it is a review item, not a verified break (pydantic's IPvAnyAddress)."""
    import ast
    import subprocess
    f = s.finding
    if "." in f.object_name or f.object_name == "<module>":
        return True
    r = subprocess.run(["git", "-C", lib_root, "show", f"{new_ref}:{f.path}"], capture_output=True, text=True)
    if r.returncode:
        return True                                     # the file is gone: the removal is real
    return not contracts.binds(r.stdout, f.object_name)


@functools.lru_cache(maxsize=512)
def _show(lib_root: str, ref: str, path: str) -> str | None:
    import subprocess
    r = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def inherited(f, lib_root: str, new_ref: str, depth: int = 3) -> bool:
    """Is the 'removed' member `Class.member` still provided at the new ref by Class itself or a base class of the
    library? Moving members up into a new base (flask 1d8b53f7, sansio App) is reported by griffe as removal."""
    import ast
    from . import reach
    parts = f.object_name.split(".")
    if len(parts) < 2:
        return False
    cls_name, member = parts[-2], parts[-1]
    mod_file = f.module.replace(".", "/")
    prefix = f.path[: f.path.rfind(mod_file)] if mod_file and mod_file in f.path else ""

    show = lambda path: _show(lib_root, new_ref, path)

    def find(module, name, d):
        for path in (f"{prefix}{module.replace('.', '/')}.py", f"{prefix}{module.replace('.', '/')}/__init__.py"):
            src = show(path)
            if src is None:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                return False
            for c in tree.body:
                if isinstance(c, ast.ClassDef) and c.name == name:
                    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in (member, "__getattr__", "__getattribute__")
                           for n in c.body) or \
                       any(member in reach.target_names(t) for n in c.body if isinstance(n, ast.Assign) for t in n.targets) or \
                       any(isinstance(n, ast.AnnAssign) and member in reach.target_names(n.target) for n in c.body) or \
                       any(isinstance(n, ast.Attribute) and n.attr == member and isinstance(n.ctx, ast.Store)
                           and isinstance(n.value, ast.Name) and n.value.id == "self" for n in ast.walk(c)):
                        return True
                    if d <= 0:
                        return False
                    imports = reach._import_map(tree, path[len(prefix):])
                    pkg = f.module.split(".")[0]
                    for b in c.bases:
                        dotted = reach._dotted(b)
                        if not dotted or dotted in ("object", "Generic", "Protocol"):
                            continue
                        q = reach.resolve(dotted, imports, {})
                        if not (q == pkg or q.startswith(pkg + ".")) and dotted.split(".")[0] in imports:
                            # a base from another package (fastapi APIRouter(starlette.routing.Router), PR #16236 removed
                            # its own `app` because Starlette's Router provides it): it may provide the member, and its
                            # source is not in this repository — so the removal cannot be verified
                            return True
                        bmod, _, bname = q.rpartition(".")
                        if find(bmod or module, bname, d - 1):
                            return True
                    return False
            return False
        return False
    return find(f.module, cls_name, depth)


def decide(s: Span, lib_root: str, old_ref: str, new_ref: str) -> Verdict | None:
    """A Verdict when a rule settles the span, None when the model must judge."""
    m, b = s.finding.message, None
    if "Public object was removed" in m and "." in s.finding.object_name and inherited(s.finding, lib_root, new_ref):
        return SKIPPED     # still provided by the class or a base at the new ref: a person decides
    if "." in s.finding.object_name and s.ambiguous and s.receiver != "self":
        return None        # a member reached through an unknown receiver that the consumer also defines: the receiver's type decides
    if "Positional parameter was moved" in m:
        b = positional.breaks(s.code, s.finding, lib_root, old_ref, new_ref, s.alias)
    elif "Parameter was added as required" in m:
        b = positional.added_required_breaks(s.code, s.finding, s.receiver, s.alias, lib_root, new_ref)
    elif "Parameter kind was changed" in m:
        b = positional.kind_changed_breaks(s.code, s.finding, s.alias)
    elif "Parameter was removed" in m and s.resolved:
        b = positional.removed_param_breaks(s.code, s.finding, s.alias, lib_root, old_ref)
    elif "Public object was removed" in m and "." in s.finding.object_name and s.receiver == "self":
        b = True          # `self.<removed member>` inside the class that lost it
    elif s.finding.object_name == "<module>" and "Public object was removed" in m:
        b = True          # importing a deleted module fails at import time
    elif "Public object was removed" in m and s.resolved and not s.ambiguous:
        # the reference resolves through an import (or the module's own name) to exactly the removed
        # object: nothing is left for the model to judge
        if not still_bound(s, lib_root, new_ref):
            return SKIPPED     # still exported at the new ref: a person decides; the model cannot see the new module
        b = True
    if b == "not_reached":
        return RULE_NOT_REACHED
    if b is None:
        return None
    return RULE_BREAK if b else RULE_COMPAT
