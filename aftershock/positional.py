"""Positional-parameter moves are decidable without a model.

griffe reports `f(param): Positional parameter was moved`. Whether a consumer breaks depends only on
how many positional arguments it passes and where each lands in the old vs new signature — which
is a signature diff, not a judgment. Calibration on click showed the model answering the wrong
question here, so this path never calls it."""
from __future__ import annotations

import ast
import subprocess

from .contracts import Finding


def parse_stmt(code: str) -> ast.AST | None:
    """Parse a statement segment; a `with f(...):`/`for`/`if` header without its body gets a `pass`."""
    code = code.strip()
    for attempt in (code, code + "\n    pass", code.rstrip(":")):
        try:
            return ast.parse(attempt).body[0]
        except (SyntaxError, IndexError):
            continue
    return None


def _is_overload(fn) -> bool:
    return any((d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")) == "overload" for d in fn.decorator_list)


def find_def(tree, object_name: str):
    """The IMPLEMENTATION of `f` / `Class.method`: `@overload` stubs are skipped — click's progressbar gained a
    keyword-only overload ahead of its implementation and the stub was read as the signature. With only
    stubs (a .pyi), the last one."""
    parts = object_name.split(".")
    if len(parts) == 2:
        bodies = [n.body for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == parts[0]]
        name = parts[1]
    elif len(parts) == 1:
        def scope(stmts):                      # module scope, through if / try / with blocks (version guards)
            out = [stmts]
            for n in stmts:
                for fld in ("body", "orelse", "finalbody"):
                    if isinstance(n, (ast.If, ast.Try, ast.With)) and getattr(n, fld, None):
                        out += scope(getattr(n, fld))
                for h in getattr(n, "handlers", []) or []:
                    out += scope(h.body)
            return out
        bodies, name = scope(tree.body), parts[0]
    else:
        return None
    defs = [fn for body in bodies for fn in body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name]
    impl = [fn for fn in defs if not _is_overload(fn)]
    return (impl or defs or [None])[-1]


def _def_at(lib_root: str, ref: str, path: str, object_name: str):
    src = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout
    if not src:
        return None
    try:
        return find_def(ast.parse(src), object_name)
    except SyntaxError:
        return None


def positional_params(lib_root: str, ref: str, path: str, object_name: str) -> list[str] | None:
    """Names of the parameters accepted positionally (excluding self) by `object_name` at `ref`."""
    fn = _def_at(lib_root, ref, path, object_name)
    return None if fn is None else [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg != "self"]


def required_params(lib_root: str, ref: str, path: str, object_name: str) -> list[str] | None:
    """Names of the parameters WITHOUT defaults (excluding self) of `object_name` at `ref`."""
    fn = _def_at(lib_root, ref, path, object_name)
    if fn is None:
        return None
    pos = [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg != "self"]
    n_def = len(fn.args.defaults)
    r = pos[: len(pos) - n_def] if n_def else pos
    return r + [a.arg for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is None]


def added_required_breaks(code: str, finding: Finding, receiver: str = "", alias: str = "",
                          lib_root: str | None = None, new_ref: str | None = None) -> bool | str | None:
    """`f(param): Parameter was added as required`. With the library available, a call breaks iff it
    supplies fewer arguments than the new signature requires (positional count + matching keywords);
    without it, the fallback is "passes nothing at all" (griffe reports `invoke(*args)` ->
    `invoke(callback, /, *args)` this way). An override breaks if its signature lacks the parameter;
    "not_reached" when the override's class does not derive from the finding's class."""
    node = parse_stmt(code)
    if node is None:
        return None
    parts = finding.object_name.split(".")
    name, cls = parts[-1], (parts[-2] if len(parts) > 1 else None)
    names = {name, alias} - {""}
    if name == "__init__" and cls:
        names.add(cls)                       # `Class.__init__(p)` is reached by calling the class: `Class(...)`
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            if not cls:
                return "not_reached"                  # a def can only override a class method
            if receiver and not any(b.split(".")[-1] == cls for b in receiver.split(",")):
                return "not_reached"
            return finding.parameter not in {a.arg for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs}
        if isinstance(n, ast.Call):
            f = n.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in names:
                kws = {k.arg for k in n.keywords}
                if any(isinstance(x, ast.Starred) for x in n.args) or None in kws:
                    return None                       # *args / **kwargs: cannot count
                req = required_params(lib_root, new_ref, finding.path, finding.object_name) if lib_root and new_ref else None
                if req is None:
                    return not (n.args or finding.parameter in kws)
                supplied = len(n.args) + sum(1 for k in kws if k in req)
                # a keyword that is not a required name still consumes nothing required; positional
                # args fill required slots left to right
                return supplied < len(req)
    return None


def removed_param_breaks(code: str, finding: Finding, alias: str = "", lib_root: str | None = None,
                         old_ref: str | None = None) -> bool | None:
    """`f(param): Parameter was removed`, on a call whose callee resolved to the library object. Breaks if
    the call passes the parameter by keyword or positionally — its index in the OLD signature within the
    positional arguments (`load_spec("x", "r1")` after its second parameter was removed: keyword-only matching
    never sees it). Passing neither: compatible. *args/**kwargs or
    an unknown old signature: None, the model judges."""
    node = parse_stmt(code)
    if node is None:
        return None
    parts = finding.object_name.split(".")
    names = {parts[-1], parts[0], alias} - {""}
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) not in names:
            continue
        kws = {k.arg for k in n.keywords}
        if finding.parameter in kws:
            return True
        if None in kws or any(isinstance(x, ast.Starred) for x in n.args):
            return None
        old = positional_params(lib_root, old_ref, finding.path, finding.object_name) if lib_root and old_ref else None
        if old is None:
            return None
        return finding.parameter in old and old.index(finding.parameter) < len(n.args)
    return None


def kind_changed_breaks(code: str, finding: Finding, alias: str = "") -> bool | None:
    """`f(param): Parameter kind was changed: positional or keyword -> positional-only` (or -> keyword-only).
    A call breaks if it passes the parameter in the form the new kind forbids (`f(name=x)` once `name` is
    positional-only)."""
    node = parse_stmt(code)
    if node is None:
        return None
    parts = finding.object_name.split(".")
    names = {parts[-1], alias} - {""}
    if parts[-1] == "__init__" and len(parts) > 1:
        names.add(parts[-2])
    to_positional_only = "positional-only" in finding.message.split("->")[-1]
    to_keyword_only = "keyword-only" in finding.message.split("->")[-1]
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) not in names:
                continue
            by_keyword = finding.parameter in {k.arg for k in n.keywords}
            if to_positional_only:
                return by_keyword
            if to_keyword_only:
                return not by_keyword and bool(n.args)   # passed positionally (approximation: any positional)
            return None
    return None


def breaks(call_code: str, finding: Finding, lib_root: str, old_ref: str, new_ref: str, alias: str = "") -> bool | None:
    """True if a positional argument of the call lands on a different parameter (or none) after the move."""
    node = parse_stmt(call_code)
    if node is None:
        return None
    call = None
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            head = finding.object_name.split(".")[0]
            if name in (head, finding.object_name.split(".")[-1], alias or head):
                call = n
                break
    if call is None:
        return None
    old = positional_params(lib_root, old_ref, finding.path, finding.object_name)
    new = positional_params(lib_root, new_ref, finding.path, finding.object_name)
    if old is None or new is None:
        return None
    k = len(call.args)
    return any(i >= len(new) or old[i] != new[i] for i in range(min(k, len(old))))
