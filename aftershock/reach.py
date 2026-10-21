"""Consumer reach, deterministic half: find candidate spans in a consumer tree that mention a finding.

A candidate is evidence that the consumer *might* be affected. Whether it actually is (the removed
parameter of the changed API, not a same-named parameter of something else) is the verification
layer's job — matching by token alone has real false positives (httpx `app=` on
transports vs clients)."""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from .contracts import Finding

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "build", "dist"}


@dataclass(frozen=True)
class Span:
    file: str
    line: int
    code: str            # the statement / call segment
    context: str         # a few lines before, through the end of the statement
    finding: Finding
    receiver: str = ""   # for an override: the enclosing class's bases; for `x.m(...)`: the receiver `x`
    alias: str = ""      # the callee's local name when it differs from the finding's (`_lk_start_span`)
    ambiguous: bool = False   # member reached through an unknown receiver AND the consumer defines a same-named member itself
    resolved: bool = False    # the callee / reference resolves to the library through imports (rules may decide; else the model)


def class_members(cls: ast.ClassDef) -> set[str]:
    """Names a class defines itself: methods, class-body assignments and annotations, and `self.x = ...` in its methods."""
    out = set()
    for b in cls.body:
        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(b.name)
        elif isinstance(b, ast.Assign):
            for t in b.targets:
                out |= target_names(t)
        elif isinstance(b, ast.AnnAssign):
            out |= target_names(b.target)
    out |= {n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
            and isinstance(n.value, ast.Name) and n.value.id == "self"}
    return out


def is_test_path(rel: str) -> bool:
    import re
    return bool(re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$", rel))


def _py_files(root: pathlib.Path):
    for p in root.rglob("*.py"):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


def _callee(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _dotted(node: ast.AST) -> str | None:
    """`click.parser.Option` -> 'click.parser.Option'; a bare name -> the name; anything else -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def target_names(t: ast.AST | None) -> set[str]:
    """Names bound by an assignment / loop / with target, through tuple and list unpacking and starred."""
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, (ast.Tuple, ast.List)):
        return set().union(*(target_names(e) for e in t.elts)) if t.elts else set()
    if isinstance(t, ast.Starred):
        return target_names(t.value)
    return set()


def bound_names(fn: ast.AST) -> set[str]:
    """Names a function binds itself: parameters (incl. *args/**kwargs), assignments, `with ... as x`,
    `for x in`, comprehensions, `except E as e`, walrus — through tuple unpacking (`for k, v in ...`)."""
    out = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = fn.args
        out |= {x.arg for x in a.args + a.posonlyargs + a.kwonlyargs} | {x.arg for x in (a.vararg, a.kwarg) if x}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out |= target_names(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
            out |= target_names(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for i in n.items:
                out |= target_names(i.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.NamedExpr):
            out |= target_names(n.target)
    return out


def module_of(rel: str) -> str:
    """Module name of a consumer file, derived like Finding.module: `libs/x/src/pkg/a/b.py` -> `pkg.a.b`."""
    parts = rel.replace("\\", "/").split("/")
    if "src" in parts:
        parts = parts[len(parts) - parts[::-1].index("src"):]
    if parts:
        parts[-1] = parts[-1].removesuffix(".py")
    while parts and not parts[0].isidentifier():
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_map(tree: ast.AST, rel: str = "") -> dict[str, str]:
    """Local name -> qualified name from this file's imports (`from click.parser import Option as O`).
    Relative imports resolve against the file's own module (`from .db_metrics import X` inside
    `pkg/providers/handlers.py` -> `pkg.providers.db_metrics.X`); one that reaches above the tree is
    left unbound, so the name stays a bare candidate."""
    m = {}
    mod = module_of(rel).split(".") if rel else []
    is_pkg = rel.endswith("__init__.py")
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                m[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                m[a.asname or a.name] = f"{n.module}.{a.name}"
        elif isinstance(n, ast.ImportFrom) and n.level > 0 and mod:
            keep = len(mod) - n.level + (1 if is_pkg else 0)
            if keep < 0:
                continue
            base = mod[:keep] + ([n.module] if n.module else [])
            for a in n.names:
                m[a.asname or a.name] = ".".join(base + [a.name])
    return m


def _library_typed(tree: ast.AST, imports: dict[str, str], exports: dict[str, str], package: str) -> dict[str, str]:
    """Names this file binds to a library instance, with the library type when it is one type:
    `spec = FieldSpec(...)`, `x: pkg.Client`, `def f(c: Client)`. A name bound to several types maps to ""."""
    out: dict[str, str] = {}
    def lib_type(node):
        d = _dotted(node) if node is not None else None
        r = resolve(d, imports, exports) if d else ""
        return r if r == package or r.startswith(package + ".") else ""
    def bind(name, t):
        out[name] = t if out.get(name, t) == t else ""
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and lib_type(n.value.func):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bind(t.id, lib_type(n.value.func))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and lib_type(n.annotation):
            bind(n.target.id, lib_type(n.annotation))
        elif isinstance(n, ast.arg) and n.annotation is not None and lib_type(n.annotation):
            bind(n.arg, lib_type(n.annotation))
    return out


def _abs_module(node: ast.ImportFrom, own_module: str, is_pkg: bool) -> str:
    """Absolute module an ImportFrom names (`from .x import y` inside pkg.a -> pkg.x)."""
    if node.level == 0:
        return node.module or ""
    mod = own_module.split(".") if own_module else []
    keep = len(mod) - node.level + (1 if is_pkg else 0)
    if keep < 0:
        return ""
    return ".".join(mod[:keep] + ([node.module] if node.module else []))


def _relative_names(tree: ast.AST, imports: dict[str, str]) -> set[str]:
    """Relative-import names that could not be resolved: kept as candidates, never dropped as foreign."""
    return {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0
            for a in n.names if (a.asname or a.name) not in imports}


def resolve(dotted: str, imports: dict[str, str], exports: dict[str, str]) -> str:
    """Qualified name of a reference: consumer imports first, then the library's top-level re-exports."""
    head, _, rest = dotted.partition(".")
    q = imports.get(head, head) + (("." + rest) if rest else "")
    pkg, _, name = q.partition(".")
    if name and "." not in name and name in exports and exports[name].startswith(pkg + "."):
        return exports[name]          # click.Option -> click.core.Option
    return q


def object_matches(node: ast.AST, finding: Finding, imports: dict[str, str], exports: dict[str, str]) -> bool:
    """Does this reference resolve to the finding's object (module-qualified), not a same-named one?"""
    d = _dotted(node)
    if d is None:
        return False
    target = finding.module + "." + finding.object_name.split(".")[0]
    resolved = resolve(d, imports, exports)
    if resolved == target:
        return True
    # a name we cannot trace to any import: fall back to the bare name (still a candidate, lower trust)
    return resolved == d and d.split(".")[-1] == target.split(".")[-1] and d not in imports


def default_changes(consumer_root: str | pathlib.Path, findings: list[Finding]) -> dict[Finding, int]:
    """For each `Parameter default was changed` finding: how many calls of that object omit the parameter."""
    root = pathlib.Path(consumer_root)
    targets = {f: (f.object_name.split(".")[-1] if f.object_name.split(".")[-1] != "__init__" else f.object_name.split(".")[0])
               for f in findings if "Parameter default was changed" in f.message}
    counts = {f: 0 for f in targets}
    for path in _py_files(root):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _callee(node)
                passed = {k.arg for k in node.keywords}
                for f, callee in targets.items():
                    if name == callee and f.parameter not in passed:
                        counts[f] += 1
    return counts


def candidates(consumer_root: str | pathlib.Path, findings: list[Finding], context_lines: int = 3,
               exports: dict[str, str] | None = None, skip_vendored: bool = False,
               modules: set[str] | frozenset = frozenset()) -> list[Span]:
    """`exports` is the library's top-level re-export map (contracts.package_exports); with it a removed
    `click.parser.Option` no longer collects every `click.Option` (which is `click.core.Option`)."""
    root = pathlib.Path(consumer_root)
    exports = exports or {}
    # keyword findings (removed/renamed parameter): match `name=` at any call site. Several objects
    # can lose the same parameter (httpx removed `proxies` from Client, AsyncClient and every
    # request function), so keep them all and pair a span with the finding whose object matches
    # the callee — pairing a `Client(proxies=)` span with the AsyncClient finding made Jev answer
    # "not this class", correctly, and the real break was missed.
    by_param: dict[str, list[Finding]] = {}
    for f in findings:
        if f.parameter and "removed" in f.message.lower():
            by_param.setdefault(f.parameter, []).append(f)

    package = findings[0].module.split(".")[0] if findings else ""

    def pick(fs: list[Finding], node: ast.Call, imports: dict[str, str], rel_names=frozenset(),
             local_classes=frozenset(), local_defs=None) -> Finding | None:
        callee = _callee(node)
        d = _dotted(node.func) or ""
        resolved = resolve(d, imports, exports) if d else ""
        for f in fs:
            head = f.object_name.split(".")[0]
            if callee in (head, f.object_name.split(".")[-1]) or resolved.split(".")[-1] == head and resolved.startswith(package + "."):
                # same name through a receiver: a module-level object needs the library as receiver
                # (`client.list_orders(region=...)` on a gRPC stub is not routers.orders.list_orders)
                if isinstance(node.func, ast.Attribute) and "." not in f.object_name:
                    r = resolve(_dotted(node.func.value) or "", imports, exports)
                    if not (r == package or r.startswith(package + ".") or (_dotted(node.func.value) or "").split(".")[0] in rel_names):
                        return None
                return f
        # `httpx.Client(http2=True)`, or `ASGITransport(app=app)` imported from httpx, is a different API
        # of the same library than the finding: not a candidate. A callee imported from anywhere else
        # (`api_pb.ListOrdersRequest(region=...)`) is another package's API. A class defined in this file
        # takes its own constructor keywords. A function defined in this file is a wrapper only if its
        # body mentions the changed object. What remains — a callee we cannot resolve — stays a candidate.
        if resolved.startswith(package + "."):
            return None
        if resolved != d and d.split(".")[0] not in rel_names:
            return None
        if callee in local_classes:
            return None
        heads = {f.object_name.split(".")[0] for f in fs} | {f.object_name.split(".")[-1] for f in fs}
        if local_defs and callee in local_defs:
            body = local_defs[callee]
            if not any((isinstance(n, ast.Name) and n.id in heads) or (isinstance(n, ast.Attribute) and n.attr in heads) for n in ast.walk(body)):
                return None
        if isinstance(node.func, ast.Attribute) and callee in def_mentions and not (def_mentions[callee] & heads):
            return None                        # `self._related_ids(limit=15)`: a method defined in this tree that never touches the library
        return fs[0]
    # object findings (removed object, positional move, new required parameter): match the object's
    # name as a reference, a call, or an override of a method with that name
    by_name: dict[str, list[Finding]] = {}
    by_name_rp: dict[str, list[Finding]] = {}     # removed parameters, matched on calls with positional args
    removed_modules = [f for f in findings if f.object_name == "<module>" and "Public object was removed" in f.message]
    for f in findings:
        if f.object_name == "<module>":
            continue
        if f.parameter and "removed" in f.message.lower():
            parts = f.object_name.split(".")
            by_name_rp.setdefault(parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1], []).append(f)
            continue
        # default-expression changes ("Attribute value was changed") are not consumer-facing contract
        # breaks and matched every `.url`/`.method` in a real run (706 candidates); skip them here
        if "Attribute value was changed" in f.message:
            continue
        # "Command: Base class was removed" restates the removed base's own finding and matched every
        # `Command(` in click's tests (76 candidates); the removed object's finding already covers it
        if "Base class was removed" in f.message:
            continue
        # a changed default is a behaviour change for every call that omits the parameter — hundreds of
        # spans per finding (290 `runner.invoke(...)` in click's tests). Reported once, in aggregate,
        # by default_changes(); never verified span by span.
        if "Parameter default was changed" in f.message:
            continue
        parts = f.object_name.split(".")
        name = parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]
        by_name.setdefault(name, []).append(f)

    def pick_by_receiver(fs: list[Finding], receiver: str) -> Finding:
        """Several classes can change the same method (ParamType/Choice/DateTime.get_metavar): prefer the
        finding whose class appears in the receiver's bases; otherwise the first."""
        bases = {b.split(".")[-1] for b in receiver.split(",") if b}
        for f in fs:
            parts = f.object_name.split(".")
            if len(parts) > 1 and parts[-2] in bases:
                return f
        return fs[0]

    spans, seen = [], set()
    # members the consumer defines on its own classes: `x.description` with an unknown `x` is then ambiguous
    # (the consumer's own `Doc.description` looks like a removed `Column.description`) — the model still
    # judges, but a "breaks" verdict is capped to needs_review
    own_members: set[str] = set()
    def_mentions: dict[str, set[str]] = {}      # every def of a name anywhere in the tree -> names its bodies mention
    for path in _py_files(root):
        try:
            t = ast.parse(path.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for c in ast.walk(t):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                def_mentions.setdefault(c.name, set()).update(
                    n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(c) if isinstance(n, (ast.Name, ast.Attribute)))
            if isinstance(c, ast.ClassDef):
                # instance attributes count too: fastapi's APIRoute sets `self.app = ...`, so `route.app` with an unknown
                # `route` is ambiguous against a removed APIRouter.app (PR #16236)
                own_members |= {n.attr for n in ast.walk(c) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                                and isinstance(n.value, ast.Name) and n.value.id == "self"}
                for b in c.body:
                    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        own_members.add(b.name)
                    elif isinstance(b, ast.AnnAssign):
                        own_members |= target_names(b.target)
                    elif isinstance(b, ast.Assign):
                        for t_ in b.targets:
                            own_members |= target_names(t_)
    for path in _py_files(root):
        rel = str(path.relative_to(root))
        # a vendored copy of the library itself (`.../httpx/_api.py`) is not a consumer; 11 of 30 sampled
        # cross-repo "consequences" were such copies
        # (only for fetched GitHub trees: inside the library's own repo the package directory is the code
        # under test, e.g. job-runner's `data/` package consumed by job-runner itself)
        if skip_vendored and package and package in pathlib.PurePosixPath(rel).parts[:-1]:
            continue
        try:
            src = path.read_text(errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        lines = src.splitlines()
        imports = _import_map(tree, rel)
        own_module = module_of(rel)
        rel_names = _relative_names(tree, imports)
        lib_typed = _library_typed(tree, imports, exports, package)
        local_classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        # module-level names this file defines itself: `REQUEST_ID_HEADER = "X-Request-ID"` is not the
        # library's constant of the same name
        def _module_scope(stmts):
            """Statements at module scope, descending into if/try/with/for blocks but never into defs or classes."""
            for n in stmts:
                yield n
                if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                    for field in ("body", "orelse", "finalbody"):
                        yield from _module_scope(getattr(n, field, []) or [])
                    for h in getattr(n, "handlers", []) or []:
                        yield from _module_scope(h.body)
        mod_stmts = list(_module_scope(tree.body))
        local_assigned = set().union(set(),
                                     *(target_names(t) for n in mod_stmts if isinstance(n, ast.Assign) for t in n.targets),
                                     *(target_names(n.target) for n in mod_stmts if isinstance(n, (ast.AnnAssign, ast.For, ast.AsyncFor))),
                                     *(target_names(i.optional_vars) for n in mod_stmts if isinstance(n, (ast.With, ast.AsyncWith)) for i in n.items))
        local_defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        def obj_ok(ref_node, f):
            # a member (`Headers.set`) is never reached by its bare name: `set(...)` is the builtin
            if "." in f.object_name and f.object_name.split(".")[-1] != "__init__":
                if isinstance(ref_node, ast.Name):
                    return False
                if isinstance(ref_node, ast.Attribute) and isinstance(ref_node.value, ast.Name) and ref_node.value.id == "self":
                    return self_member_ok(ref_node, f)
            # a bare name this file defines itself (`def build_image` in the file that calls build_image) is not the
            # library's same-named object, whatever kind of change was reported (Prefect e977a2ca)
            d_ = _dotted(ref_node)
            obj = f.object_name.removesuffix(".__init__")      # a constructor is called by its class's name
            if d_ and "." not in d_ and "." not in obj and d_ not in imports and own_module != f.module \
                    and (d_ in local_assigned or d_ in local_classes or d_ in local_defs):
                return False
            # a module-level object reached through a receiver (`admin.create_user(...)`): the receiver must
            # be the library itself (`import httpx; httpx.create_ssl_context`, `from pkg.routes import admin`).
            # A local object's method of the same name is not it (`AdminService.update_user` is not the module
            # function `pkg.routes.admin.update_user`). A name bound by a relative import cannot be resolved,
            # so it stays a candidate.
            if "." not in f.object_name and isinstance(ref_node, ast.Attribute):
                d = _dotted(ref_node.value)
                r = resolve(d, imports, exports) if d else ""
                # the receiver must be the module that defines the object (or a package above it that may re-export it):
                # `prefect.context.SyncClientContext.get()` is not the removed function prefect.cli.cloud.webhook.get
                if not (r == f.module or f.module.startswith(r + ".") or (d or "").split(".")[0] in rel_names):
                    return False
            # removed objects must resolve to the finding's module. Any other name match is dropped when
            # it resolves to a module outside the library (`from logging_config import start_span` is the
            # consumer's own wrapper, not logkit's); an unresolvable bare name stays a candidate.
            if "Public object was removed" in f.message:
                d0 = _dotted(ref_node)
                if d0 and "." not in d0 and own_module != f.module and (d0 in local_assigned or d0 in local_classes or d0 in local_defs):
                    return False                      # this file's own same-named object (unless the file IS the changed module)
                head0 = (d0 or "").split(".")[0]
                if head0 and head0 not in imports and shadowed(ref_node, head0):
                    return False                      # a local variable of the enclosing function
                return object_matches(ref_node, f, imports, exports)
            d = _dotted(ref_node)
            if d:
                resolved = resolve(d, imports, exports)
                if resolved != d and not resolved.startswith(package + "."):
                    return False
                # traced through an import to a same-named object of ANOTHER module of the library
                # (`from prefect.task_worker import serve` is not `prefect.cli.task.serve`); a package above the
                # finding's module may re-export it, so only unrelated modules are dropped
                if resolved != d and "." not in f.object_name:
                    rmod, _, rname = resolved.rpartition(".")
                    if rname == f.object_name and rmod != f.module and not f.module.startswith(rmod + "."):
                        return False
            return True

        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

        bound_cache: dict = {}

        def shadowed(node, name: str) -> bool:
            """Is `name` bound by an enclosing function of `node` (`async with AsyncClient() as client: client.send()`
            is not the removed module-level `client`)?"""
            p = parents.get(node)
            while p is not None:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if p not in bound_cache:
                        bound_cache[p] = bound_names(p)
                    if name in bound_cache[p]:
                        return True
                p = parents.get(p)
            return False

        def enclosing_class(node) -> ast.ClassDef | None:
            while node is not None:
                node = parents.get(node)
                if isinstance(node, ast.ClassDef):
                    return node
            return None

        def receiver_of(node) -> str:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = parents.get(node)
                if isinstance(cls, ast.ClassDef):
                    return ",".join(_dotted(b) or "?" for b in cls.bases)
                return ""
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                return _dotted(node.func.value) or "?"
            if isinstance(node, ast.Attribute):
                return _dotted(node.value) or "?"
            return ""

        def same_module(a: str, b: str) -> bool:
            """`pkg.mod` and `mod` name the same module when the tree was scanned from inside the package."""
            return a == b or a.endswith("." + b) or b.endswith("." + a)

        def self_member_ok(node: ast.Attribute, f: Finding) -> bool:
            """`self.member` is the finding's member only inside its class, or a subclass that does not define it."""
            cls_name, member = f.object_name.split(".")[-2:]
            cls = enclosing_class(node)
            if cls is None:
                return False
            if cls.name == cls_name and same_module(own_module, f.module):
                return True                                 # the finding's own class
            if member in class_members(cls):
                return False      # defines the member itself: a subclass's override, or pydantic/v1's own copy of the class
            # a subclass, or a same-named class elsewhere that leaves the member to a subclass (werkzeug's sansio Request)
            return cls.name == cls_name or any((_dotted(b) or "").split(".")[-1] == cls_name for b in cls.bases)

        def member_ok(node: ast.Attribute, f: Finding) -> bool:
            """`Class.member` finding: `self.member` counts only inside that class (or a subclass); any
            other receiver is a candidate whose type the model must judge from the evidence."""
            cls_name = f.object_name.split(".")[-2]
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                return self_member_ok(node, f)
            # a receiver imported from outside the library is the consumer's own object, not the finding's
            # class: `settings.url` with `from app.config import settings` while the removed member lives on
            # `lib.config.Settings`. The evidence shown to the model does not include the import, so this
            # cannot be left to it
            d = _dotted(node.value)
            if d:
                resolved = resolve(d, imports, exports)
                if resolved in modules:
                    return False                      # a module of the library is not an instance of the finding's class
                if resolved != d and not resolved.startswith(package + "."):
                    return False
                # the receiver resolves to a named object of the library that is NOT the finding's class:
                # `Plan.limit` (another class's column) is not the removed `Customer.limit`
                head = d.split(".")[0]
                if head in imports and resolved != f.module + "." + cls_name and resolved.split(".")[-1] != cls_name \
                        and resolved.split(".")[-1][:1].isupper():
                    return False
            return True

        def add(node, finding, alias=""):
            end = getattr(node, "end_lineno", node.lineno)
            key = (rel, node.lineno, finding.symbol)
            if key in seen:
                return
            seen.add(key)
            recv = receiver_of(node)
            amb = False
            if "." in finding.object_name and recv and recv != "self" and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                r = resolve(recv, imports, exports)
                typed = lib_typed.get(recv, "")
                if typed and typed.split(".")[-1] != finding.object_name.split(".")[-2]:
                    amb = True        # an instance of another library class: `url_map = Map(...)` is not Headers
                else:
                    amb = (not (r == package or r.startswith(package + ".")) and recv.split(".")[0] not in lib_typed
                           and finding.object_name.split(".")[-1] in own_members)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                spans.append(Span(rel, node.lineno, "\n".join(lines[node.lineno - 1:end]),
                                  "\n".join(lines[max(0, node.lineno - 1 - context_lines):end]), finding, "", alias, False, True))
                return
            target = node.func if isinstance(node, ast.Call) else node
            dt = _dotted(target) or ""
            rt = resolve(dt, imports, exports) if dt else ""
            head_imported = bool(dt) and dt.split(".")[0] in imports          # `import httpx; httpx.Client` resolves to itself
            # the changed module itself: a reference to a removed module-level object resolves; a same-named MEMBER does not
            # (pydantic/types.py defines many `decode`/`encode`/`__hash__` on other classes)
            res = (bool(dt) and head_imported and (rt == package or rt.startswith(package + "."))) or (own_module == finding.module and "." not in finding.object_name)
            # a removed MEMBER (`Class.get`) reached through a non-self receiver is the class's member only when the receiver
            # IS the class: `_cv_app.get(None)` resolves into the library (a ContextVar) and is no use of SecureCookieSession.get
            # (Flask daca74d9: 18 verified matches of every `.get(`). Anything else is for the model or a person.
            if "Public object was removed" in finding.message and "." in finding.object_name:
                cls_ = finding.object_name.split(".")[-2]
                res = bool(recv) and recv != "self" and resolve(recv, imports, exports) == f"{finding.module}.{cls_}"
            spans.append(Span(rel, node.lineno, "\n".join(lines[node.lineno - 1:end]),
                              "\n".join(lines[max(0, node.lineno - 1 - context_lines):end]), finding,
                              recv, alias, amb, res))

        paired = set()                      # callees of calls already paired with a finding
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                # `from pkg.mod import Obj` where Obj was removed from pkg.mod: this line raises first, before any
                # use runs (in executed-truth runs it is the frame that fails)
                mod = _abs_module(node, own_module, rel.endswith("__init__.py"))
                for al in node.names:
                    for f in by_name.get(al.name, []):
                        if "Public object was removed" in f.message and "." not in f.object_name and mod == f.module:
                            add(node, f)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and removed_modules:
                # `pkg.mod.<module>: Public object was removed` — every import of that module fails at import time
                if isinstance(node, ast.ImportFrom):
                    mod = _abs_module(node, own_module, rel.endswith("__init__.py"))
                    hits = [f for f in removed_modules if mod == f.module or any(f"{mod}.{a.name}" == f.module for a in node.names)]
                else:
                    hits = [f for f in removed_modules if any(a.name == f.module for a in node.names)]
                for f in hits:
                    add(node, f)
                continue
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in by_param:
                        f = pick(by_param[kw.arg], node, imports, rel_names, local_classes, local_defs)
                        if f is not None:
                            add(node, f)
                            parts_ = f.object_name.split(".")
                            if _callee(node) not in {parts_[0], parts_[-1]} and spans and spans[-1].line == node.lineno \
                                    and spans[-1].finding is f:
                                # an unresolved callee of another name (`plot(..., kind=kind)` vs WrappedCythonOp(kind=),
                                # pandas 1a561f8f): it may be a wrapper, so it stays a candidate — but the evidence cannot
                                # show what it calls, so a model 'breaks' is capped to review
                                import dataclasses
                                spans[-1] = dataclasses.replace(spans[-1], ambiguous=True)
                # positional moves: only calls that pass positional arguments can break; keyword-only
                # calls are safe by construction. New required parameters / removed objects: any call.
                # ponytail: griffe's oneline output does not give the moved parameter's index, so any
                # call with >=2 positional args is a candidate; parse `-f verbose` for the index later.
                name, alias = _callee(node), ""
                if name not in by_name and name not in by_name_rp:
                    # aliased import: `from lib import start_span as _start_span` — the consumer's call sites use
                    # the alias, not the library's name
                    d = _dotted(node.func)
                    resolved = resolve(d, imports, exports) if d else ""
                    if resolved.startswith(package + ".") and (resolved.split(".")[-1] in by_name or resolved.split(".")[-1] in by_name_rp):
                        alias, name = name, resolved.split(".")[-1]
                if name in by_name:
                    # several classes can change the same member: pair the call with the first one it can reach
                    first = pick_by_receiver(by_name[name], receiver_of(node))
                    for f in [first] + [x for x in by_name[name] if x is not first]:
                        if ("Positional parameter was moved" not in f.message or len(node.args) >= 1) and obj_ok(node.func, f):
                            add(node, f, alias)
                            paired.add(node.func)     # its callee is not paired again as an attribute
                            break
                if name in by_name_rp and len(node.args) >= 1:
                    # a removed parameter passed positionally: `load_spec("x", "r1")` after its second parameter
                    # was removed. The keyword path above cannot see it.
                    for f in by_name_rp[name]:
                        if obj_ok(node.func, f):
                            add(node, f, alias)
            elif isinstance(node, ast.Attribute) and node.attr in by_name and node not in paired and not (
                    isinstance(node.ctx, ast.Store) and not isinstance(parents.get(node), ast.AugAssign)):
                # `self.name = ...` writes the attribute (huggingface_hub ffc07b64: a class's own __init__ was matched as
                # uses of its removed fields); `self.n += 1` reads it first and stays a use
                for f in by_name[node.attr]:        # the first same-named finding this reference can reach
                    is_member = "." in f.object_name and "Public object was removed" in f.message
                    if (member_ok(node, f) if is_member else obj_ok(node, f)):
                        add(node, f)
                        break
            elif isinstance(node, ast.Name) and node.id in by_name and isinstance(node.ctx, ast.Load) and obj_ok(node, by_name[node.id][0]):
                par = parents.get(node)
                if isinstance(par, ast.Call) and _callee(par) in ("Depends", "Security") and node in par.args:
                    continue                          # FastAPI injects its own arguments: `Depends(get_service)` is not a call by the consumer
                add(node, by_name[node.id][0])       # a Store is the object's own (re)definition, not a use
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in by_name and receiver_of(node) \
                    and any("." in f.object_name for f in by_name[node.name]):
                # a method override on a class (bases known); a module-level def with the same name is the
                # consumer's own function (its own wrapper of the same name), not a use of the library's
                fsig = [f for f in by_name[node.name] if "." in f.object_name and "Public object was removed" not in f.message]
                if fsig:
                    # an override matters for a SIGNATURE change; a def named like a removed member is that class's own
                    # method, not a use of the removed one (werkzeug mixins `def popitem`)
                    add(node, pick_by_receiver(fsig, receiver_of(node)))
    return spans
