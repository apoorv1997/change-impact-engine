"""Contract diff: what changed in a library's public API between two git refs.

Deterministic. Wraps `griffe check` rather than re-implementing API diffing; griffe found 37/38 and
43/46 real breaks on httpx 0.28 / click 8.2 in a pre-test."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): (?P<symbol>[^:]+): (?P<message>.+)$")
_COSMETIC = ("__version__", "TypeVar(")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    symbol: str          # e.g. "Client.__init__(proxies)" or "BaseCommand"
    message: str         # griffe's message, e.g. "Parameter was removed"

    @property
    def object_name(self) -> str:
        """`Client.__init__(proxies)` -> `Client.__init__`; `BaseCommand` -> `BaseCommand`."""
        return self.symbol.split("(")[0]

    @property
    def module(self) -> str:
        """`src/click/parser.py` -> `click.parser`; `httpx/_client.py` -> `httpx._client`."""
        parts = self.path.replace("\\", "/").split("/")
        if "src" in parts:                       # python/log-kit/src/logkit/tracing.py -> logkit.tracing
            parts = parts[len(parts) - parts[::-1].index("src"):]
        if parts:
            parts[-1] = parts[-1].removesuffix(".py")   # before the identifier check: `job-runner/main.py` -> `main`
        while parts and not parts[0].isidentifier():   # drop non-importable prefixes such as `log-kit`
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    @property
    def qualified(self) -> str:
        """Fully qualified changed object, e.g. `click.parser.Option` or `httpx._client.Client.__init__`."""
        return f"{self.module}.{self.object_name}"

    @property
    def parameter(self) -> str | None:
        m = re.search(r"\((\w+)\)$", self.symbol)
        return m.group(1) if m else None

    @property
    def search_tokens(self) -> list[str]:
        """Tokens a consumer scan looks for: the parameter as `name=` when there is one, else the object's last component."""
        if self.parameter:
            return [f"{self.parameter}="]
        return [self.object_name.split(".")[-1]]

    def describe(self) -> str:
        return f"{self.qualified}{self.symbol[len(self.object_name):]}: {self.message}"


def package_exports(lib_root: str, package: str, ref: str, search: str | None = None) -> dict[str, str]:
    """Top-level re-exports of the package at `ref`: name -> defining module (from `<package>/__init__.py`).

    `click.Option` is `click.core.Option`, not the removed `click.parser.Option`; without this map a
    consumer scan cannot tell them apart and neither can the model (click calibration)."""
    import ast
    rel = f"{search + '/' if search else ''}{package}/__init__.py"
    src = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{rel}"], capture_output=True, text=True).stdout
    out: dict[str, str] = {}
    if not src:
        return out
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = f"{package}.{node.module}" if node.level else node.module
            for a in node.names:
                out[a.asname or a.name] = f"{mod}.{a.name}"
    return out


def parse_oneline(text: str, drop_cosmetic: bool = True) -> list[Finding]:
    out = []
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        if drop_cosmetic and any(c in raw for c in _COSMETIC):
            continue
        out.append(Finding(m["path"], int(m["line"]), m["symbol"], m["message"]))
    return out


def _griffe_exe() -> str:
    """griffe from the same environment as this interpreter, even when the venv is not on PATH."""
    import pathlib, shutil, sys
    local = pathlib.Path(sys.executable).parent / "griffe"
    return str(local) if local.exists() else (shutil.which("griffe") or "griffe")


def griffe_findings(package: str, old_ref: str, new_ref: str, cwd: str, search: str | None = None,
                    timeout: float | None = None) -> list[Finding]:
    cmd = [_griffe_exe(), "check", package, "-a", old_ref, "-b", new_ref, "-f", "oneline", "--no-color"]
    if search:
        cmd += ["-s", search]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    # griffe prints findings on stderr and exits 1 when it finds breakages
    return parse_oneline(r.stdout + "\n" + r.stderr)


def binds(src: str, name: str) -> bool:
    """Does this module source bind `name` anywhere — def/class, assignment (any branch, `if TYPE_CHECKING` too),
    import, or by name in `__all__` / a lazy-export table (`_dynamic_imports = {"X": (...)}`)? Unparseable = unknown = True."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return True
    for n in ast.walk(tree):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return True
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
            return True
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) == name for a in n.names):
            return True
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(isinstance(t, ast.Name) and t.id in ("__all__", "_dynamic_imports")
                                                              for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
            if n.value is not None and any(isinstance(c, ast.Constant) and c.value == name for c in ast.walk(n.value)):
                return True
    return False


def library_modules(lib_root: str, package: str, ref: str, search: str | None = None) -> set[str]:
    """Dotted names of every module and package of the library at `ref` (`prefect`, `prefect.context`, ...).
    A receiver that resolves to one of these is a module, never an instance of a class: `prefect.context.X` cannot
    be a member of `TaskRunResponse` (Prefect 10a5fd26: 26 false 'removed field' matches)."""
    pkg_dir = f"{search + '/' if search else ''}{package}"
    files = subprocess.run(["git", "-C", lib_root, "ls-tree", "-r", "--name-only", ref, pkg_dir],
                           capture_output=True, text=True).stdout.split()
    base = f"{search}/" if search else ""
    out: set[str] = set()
    for f in files:
        if not f.endswith(".py") or not f.startswith(base):
            continue
        parts = f[len(base):-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def commits_behind(lib_root: str, old_ref: str, new_ref: str, path: str) -> tuple[int, int]:
    """(commits on old_ref not in new_ref, commits on new_ref not in old_ref) that touch `path`.

    A signature diff sees API changes only; a branch whose copy of a library is *behind* the trunk
    also lacks every behavioural fix made since. Reported as a lag, not a finding."""
    def count(rng):
        r = subprocess.run(["git", "-C", lib_root, "rev-list", "--count", rng, "--", path], capture_output=True, text=True)
        return int(r.stdout.strip() or 0)
    return count(f"{new_ref}..{old_ref}"), count(f"{old_ref}..{new_ref}")


def public_names(lib_root: str, package: str, ref: str, search: str | None = None) -> set[str]:
    """Every top-level class/function name and module name in the package at `ref`, plus re-exports —
    the vocabulary an explanation may legitimately propose as a replacement API."""
    import ast
    pkg_dir = f"{search + '/' if search else ''}{package}"
    files = subprocess.run(["git", "-C", lib_root, "ls-tree", "-r", "--name-only", ref, pkg_dir],
                           capture_output=True, text=True).stdout.split()
    names: set[str] = set()
    for path in files:
        if not path.endswith(".py"):
            continue
        names.add(path.split("/")[-1].removesuffix(".py"))
        src = subprocess.run(["git", "-C", lib_root, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
                if isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            names.add(sub.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    names.add((a.asname or a.name).split(".")[-1])
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names
