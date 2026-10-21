"""The smallest checks that fail if the deterministic layers break. No network."""
import pathlib
import textwrap

from aftershock import config_contract, contracts, evidence, reach

ONELINE = """\
httpx/__version__.py:3: __version__: Attribute value was changed: '0.27.2' -> '0.28.0'
httpx/_client.py:641: Client.__init__(proxies): Parameter was removed
src/click/core.py:0: BaseCommand: Public object was removed
src/click/termui.py:328: progressbar(show_eta): Positional parameter was moved
"""


def test_parse_oneline_drops_cosmetic_and_splits_symbol():
    f = contracts.parse_oneline(ONELINE)
    assert [x.symbol for x in f] == ["Client.__init__(proxies)", "BaseCommand", "progressbar(show_eta)"]
    assert f[0].parameter == "proxies" and f[0].object_name == "Client.__init__"
    assert f[1].parameter is None and f[1].search_tokens == ["BaseCommand"]


def test_candidates_find_keyword_object_and_override(tmp_path: pathlib.Path):
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        import httpx, click
        client = httpx.AsyncClient(app=app)          # keyword on the changed object
        t = httpx.ASGITransport(app=app)             # same keyword, different object of the library -> excluded
        class Cmd(click.BaseCommand): pass           # removed object
        class T(click.ParamType):
            def get_metavar(self, param): ...        # override of a changed method
    """))
    f = contracts.parse_oneline(
        "src/httpx/_client.py:1: AsyncClient.__init__(app): Parameter was removed\n"
        "src/click/core.py:0: BaseCommand: Public object was removed\n"
        "src/click/types.py:1: ParamType.get_metavar(ctx): Parameter was added as required\n")
    spans = reach.candidates(tmp_path, f)
    got = {(s.line, s.finding.symbol) for s in spans}
    assert (3, "AsyncClient.__init__(app)") in got and (4, "AsyncClient.__init__(app)") not in got
    assert any(sym == "BaseCommand" for _, sym in got)
    assert any(sym == "ParamType.get_metavar(ctx)" for _, sym in got)


def test_strip_comments_keeps_code_and_strings():
    src = "x = httpx.Client(app=app)  # app goes to the transport\ns = '# not a comment'\n"
    out = evidence.strip_comments(src)
    assert "transport" not in out and "x = httpx.Client(app=app)" in out and "'# not a comment'" in out


def test_config_contract_sets_vs_reads(tmp_path: pathlib.Path):
    (tmp_path / "app.py").write_text('import os\nA = os.getenv("A_KEY")\nB = os.environ["B_KEY"]\n')
    (tmp_path / "values.yaml").write_text("env:\n  A_KEY: '1'\n  C_KEY: '3'\n")
    c = config_contract.scan(tmp_path, [tmp_path / "values.yaml"])
    assert c.set_but_unread == {"C_KEY"} and c.read_but_unset == {"B_KEY"}


def test_resolve_separates_reexport_from_removed_module_object():
    exports = {"Option": "click.core.Option", "BaseCommand": "click.core.BaseCommand"}
    imports = {"click": "click", "POpt": "click.parser.Option"}
    assert reach.resolve("click.Option", imports, exports) == "click.core.Option"
    assert reach.resolve("click.parser.Option", imports, exports) == "click.parser.Option"
    assert reach.resolve("POpt", imports, exports) == "click.parser.Option"
    assert reach.resolve("click.BaseCommand", imports, exports) == "click.core.BaseCommand"
    f = contracts.parse_oneline("src/click/parser.py:0: Option: Public object was removed")[0]
    assert f.module == "click.parser" and f.qualified == "click.parser.Option"


def test_added_required_override_needs_matching_class():
    from aftershock import positional
    f = contracts.parse_oneline("src/click/core.py:748: Context.invoke(callback): Parameter was added as required")[0]
    code = "def invoke(self, ctx):\n    ctx.invoke(self.callback, ctx.args, **ctx.params)"
    assert positional.added_required_breaks(code, f, receiver="click.Command") == "not_reached"
    assert positional.added_required_breaks(code, f, receiver="click.Context") is True
    assert positional.added_required_breaks("ctx.invoke(cb)", f, receiver="ctx") is False
    assert positional.added_required_breaks("ctx.invoke()", f, receiver="ctx") is True


def test_budget_stops_calls_at_cap(tmp_path, monkeypatch):
    from aftershock import budget
    monkeypatch.setattr(budget, "LEDGER", tmp_path / "spend.jsonl")
    b = budget.Budget(run_usd=0.0001, total_usd=10)          # ~2 calls of 800 tokens
    allowed = []
    for _ in range(5):
        ok = b.allow(); allowed.append(ok)
        if ok:
            b.record(800)
    assert allowed == [True, True, False, False, False] and b.skipped == 3
    b.close("t"); assert budget.Budget(run_usd=1, total_usd=10).prior_usd > 0


def test_vendored_copy_and_same_library_lookalike_are_not_candidates(tmp_path: pathlib.Path):
    (tmp_path / "httpx").mkdir()
    (tmp_path / "httpx" / "_api.py").write_text("def get(url, proxies=None):\n    return request('GET', url, proxies=proxies)\n")
    (tmp_path / "app.py").write_text(textwrap.dedent("""
        import httpx
        from httpx import ASGITransport, AsyncClient
        c1 = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")   # look-alike, same library
        c2 = AsyncClient(app=app)                                                # the real thing
        c3 = httpx.Client(http2=True)                                            # other API of the library
    """))
    f = contracts.parse_oneline("httpx/_client.py:1: AsyncClient.__init__(app): Parameter was removed\n"
                                "httpx/_config.py:1: create_ssl_context(http2): Parameter was removed\n"
                                "httpx/_api.py:1: request(proxies): Parameter was removed\n")
    spans = reach.candidates(tmp_path, f, exports={"ASGITransport": "httpx._transports.asgi.ASGITransport"}, skip_vendored=True)
    assert [(s.file, s.line) for s in spans] == [("app.py", 5)]


def test_kind_changed_to_positional_only_breaks_keyword_callers():
    from aftershock import positional
    f = contracts.parse_oneline("python/log-kit/src/logkit/tracing.py:106: start_span(name): "
                                "Parameter kind was changed: positional or keyword -> positional-only")[0]
    assert positional.kind_changed_breaks('with start_span(name="build"):\n    pass', f) is True
    assert positional.kind_changed_breaks('with start_span("build"):\n    pass', f) is False
    assert positional.kind_changed_breaks('other(name="x")', f) is None


def test_aliased_import_call_is_a_candidate_and_wrapper_def_is_not(tmp_path: pathlib.Path):
    (tmp_path / "logging_config.py").write_text(textwrap.dedent("""
        from logkit import start_span as _lk_start_span

        def start_span(name, **tags):          # the consumer's own wrapper: not a use of the library
            return _lk_start_span(name, **tags)
    """))
    (tmp_path / "pipeline.py").write_text("from logging_config import start_span\nwith start_span(name='x'):\n    pass\n")
    f = contracts.parse_oneline("python/log-kit/src/logkit/tracing.py:106: start_span(name): "
                                "Parameter kind was changed: positional or keyword -> positional-only")
    spans = reach.candidates(tmp_path, f)
    assert [(s.file, s.line) for s in spans] == [("logging_config.py", 5)]


def test_removed_member_matches_self_inside_class_and_unknown_receivers(tmp_path: pathlib.Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "fetcher.py").write_text(textwrap.dedent("""
        class Loader:
            def run(self):
                return self.load_daily      # inside the class: candidate, decided by rule
        class Other:
            def run(self):
                return self.load_daily      # a different class's own attribute: not a candidate
        def use(f):
            return f.load_daily             # unknown receiver: candidate for the model
    """))
    f = contracts.parse_oneline("job-runner/data/fetcher.py:0: Loader.load_daily: Public object was removed")
    spans = reach.candidates(tmp_path, f)
    assert sorted((s.line, s.receiver) for s in spans) == [(4, "self"), (9, "f")]


def test_named_query_contract_rules(tmp_path: pathlib.Path):
    from aftershock import named_queries as nq
    old = {"q_orders": nq.QuerySig("q_orders", ("ids_in",), ("limit",)), "q_gone": nq.QuerySig("q_gone", (), ())}
    new = {"q_orders": nq.QuerySig("q_orders", ("ids_in", "region"), ())}
    kinds = sorted((c.name, c.kind) for c in nq.diff(old, new))
    assert kinds == [("q_gone", "removed"), ("q_orders", "param_removed"), ("q_orders", "required_added")]
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        r = s.post(query_url("q_orders"), json={"params": {"ids_in": u}})
        self._fetch_named("q_orders", {"ids_in": u, "region": m}, "t")
        self._fetch_named("q_orders", build_params(), "t")
        self._fetch_named("q_gone", {}, "t")
    """))
    v = {(x.site.line, x.status) for x in nq.verify(nq.call_sites(tmp_path), old, new)}
    assert v == {(2, "breaks_consumer"), (3, "compatible"), (4, "needs_review"), (5, "breaks_consumer")}


def test_named_queries_absent_registry_is_not_a_total_removal(capsys):
    from aftershock import cli
    import types
    a = types.SimpleNamespace(lib=".", old="x", new="y", src="nope", consumer=".", no_tests=True)
    cli.named_queries = types.SimpleNamespace(registry=lambda lib, ref, src: {"q": None} if ref == "x" else {},
                                              diff=None, call_sites=None, verify=None)
    cli.named_queries_cmd(a)
    assert "ABSENT" in capsys.readouterr().out


def test_wrapper_injected_keys_count_as_sent(tmp_path: pathlib.Path):
    from aftershock import named_queries as nq
    (tmp_path / "_base.py").write_text(textwrap.dedent("""
        async def _named_query(self, query_name, params):
            clean = {k: v for k, v in params.items() if v is not None}
            markets = resolve()
            if markets is not None:
                clean["region_groups"] = markets
            body = {"params": clean}
            return await self._post(f"/q/{query_name}", body)
    """))
    (tmp_path / "accounts.py").write_text('rows = await self._named_query("account_calendar", {"account_id": u})\n')
    inj = nq.injected_keys(tmp_path)
    assert "region_groups" in inj and inj["region_groups"].conditional
    old = {"account_calendar": nq.QuerySig("account_calendar", ("account_id",), ())}
    new = {"account_calendar": nq.QuerySig("account_calendar", ("account_id", "region_groups"), ())}
    v = nq.verify(nq.call_sites(tmp_path, names=set(new)), old, new, injected=inj)
    assert [x.status for x in v] == ["compatible"] and "via wrapper" in v[0].reason


def test_explanation_input_and_hallucination_check():
    from aftershock import explain
    kept = [{"file": "a.py", "line": 3, "finding": "x.f(p): Parameter was removed", "how": "rule", "code": "f(p=1)"}]
    review = [{"file": "b.py", "line": 9, "finding": "x.g: Public object was removed", "reason": "band"}]
    inp = explain.build_input({"library": "x"}, kept, review)
    assert inp["verified_consequences"][0]["location"] == "a.py:3" and inp["for_a_human"][0]["location"] == "b.py:9"
    allowed = {"a.py:3", "b.py:9"}
    assert explain.unknown_locations("`a.py:3` breaks; also see b.py:9 and c.py:12 and a.py:4.", allowed) == ["a.py:4", "c.py:12"]
    assert explain.unknown_locations("nothing referenced", allowed) == []


def test_explanation_skips_when_input_alone_exceeds_cap():
    from aftershock import explain
    big = [{"file": "a.py", "line": i, "finding": "f", "how": "rule", "code": "x" * 1200} for i in range(200)]
    ex = explain.explain({"library": "x"}, big, [], max_usd=0.0001, client=object(), backend="anthropic")   # cap trips before any network
    assert "skipped" in ex.text and ex.usd == 0.0


def test_template_explanation_names_mechanism_and_locations():
    from aftershock import explain
    kept = [{"file": "svc/a.py", "line": 3, "finding": "httpx._client.Client.__init__(proxies): Parameter was removed",
             "how": "calibrated check", "code": "client = httpx.Client(proxies=p)"},
            {"file": "svc/b.py", "line": 8, "finding": "click.parser.OptionParser: Public object was removed",
             "how": "rule", "code": "p = OptionParser(ctx)"}]
    ex = explain.explain({"library": "httpx", "old_version": "0.27.2", "new_version": "0.28.0"}, kept, [], backend="template")
    assert ex.usd == 0 and "svc/a.py:3" in ex.text and "svc/b.py:8" in ex.text
    assert "unexpected keyword argument 'proxies'" in ex.text and "ImportError" in ex.text
    assert explain.unknown_locations(ex.text, {"svc/a.py:3", "svc/b.py:8"}) == []


def test_invented_api_check_flags_names_the_new_library_lacks():
    from aftershock import explain
    names = {"Client", "AsyncClient", "Proxy", "HTTPTransport"}
    text = "Replace `httpx.Client(proxies=p)` with `httpx.Client(proxy=p)`; or use `httpx.Proxies(...)` / `httpx.Proxy(...)`."
    assert explain.unknown_apis(text, "httpx", names) == ["httpx.Proxies"]


def test_flag_appends_omitted_verified_locations_and_overused_review_items():
    from aftershock import explain
    text = "`a.py:1` breaks. `r.py:9` also breaks badly.\n\nFor a human:\n- `r.py:9` check it"
    out, bad, apis = explain._flag(text, {"a.py:1", "b.py:2", "r.py:9"}, {"library": "x"},
                                   kept_locs={"a.py:1", "b.py:2"}, review_locs={"r.py:9"})
    assert bad == [] and "`b.py:2`" in out and "did not list" in out and "not verified consequences" in out


def test_member_receiver_imported_from_another_package_is_not_a_candidate(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="services/web-api/src/web_api/config.py", line=1, symbol="ApiSettings.packaging_url",
                message="Public object was removed")
    (tmp_path / "own.py").write_text("from web_engine.config import settings\nurl = settings.packaging_url\n")
    (tmp_path / "theirs.py").write_text("from web_api.config import settings\nurl = settings.packaging_url\n")
    (tmp_path / "bare.py").write_text("url = settings.packaging_url\n")
    got = {s.file for s in reach.candidates(tmp_path, [f])}
    assert got == {"theirs.py", "bare.py"}, got


def test_module_level_function_through_a_local_receiver_is_not_a_candidate(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="services/auth-svc/src/auth_svc/routes/admin.py", line=0, symbol="create_user(plans)",
                message="Parameter was added as required")
    (tmp_path / "local.py").write_text("async def route(admin):\n    return await admin.create_user(name='x')\n")
    (tmp_path / "module.py").write_text("from auth_svc.routes import admin\nadmin.create_user(name='x')\n")
    (tmp_path / "src/auth_svc").mkdir(parents=True)
    (tmp_path / "src/auth_svc/relative.py").write_text("from .routes import admin\nadmin.create_user(name='x')\n")
    (tmp_path / "method.py").write_text("class Repo:\n    def create_user(self, name):\n        pass\n")
    got = {s.file.split("/")[-1] for s in reach.candidates(tmp_path, [f])}
    assert got == {"module.py", "relative.py"}, got


def test_relative_imports_resolve_against_the_file_module(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="libs/core/src/pkg/providers/metrics.py", line=0, symbol="DAILY_METRICS", message="Public object was removed")
    d = tmp_path / "libs/core/src/pkg/providers"; d.mkdir(parents=True)
    (d / "handlers.py").write_text("from .metrics import DAILY_METRICS\nx = DAILY_METRICS\n")           # the removed object itself
    (d / "moved.py").write_text("from .catalog import DAILY_METRICS\nx = DAILY_METRICS\n")         # already imported from its new home
    got = {s.file.split("/")[-1] for s in reach.candidates(tmp_path, [f])}
    assert got == {"handlers.py"}, got
    assert reach.module_of("libs/core/src/pkg/providers/handlers.py") == "pkg.providers.handlers"
    assert reach._import_map(__import__("ast").parse("from .. import util"), "pkg/a/b.py") == {"util": "pkg.util"}


def test_keyword_findings_skip_foreign_callees_local_classes_and_non_wrappers(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/pkg/routers/orders.py", line=0, symbol="list_orders(region)", message="Parameter was removed")
    (tmp_path / "stub.py").write_text("async def go(client):\n    return await client.list_orders(date=d, region=m)\n")
    (tmp_path / "foreign.py").write_text("from other.protos import api_pb2 as api_pb\nreq = api_pb.ListOrdersRequest(region=m)\n")
    (tmp_path / "localcls.py").write_text("class _Doc:\n    pass\nd = _Doc(region=m)\n")
    (tmp_path / "nonwrap.py").write_text("def helper(a, *, region):\n    return a\nhelper(1, region=m)\n")
    (tmp_path / "wrapper.py").write_text("from pkg.routers import orders\ndef helper(**kw):\n    return orders.list_orders(**kw)\nhelper(region=m)\n")
    (tmp_path / "direct.py").write_text("from pkg.routers.orders import list_orders\nlist_orders(region=m)\n")
    (tmp_path / "alias.py").write_text("from pkg.routers.orders import list_orders as ls\nls(region=m)\n")
    (tmp_path / "unknown.py").write_text("mystery(region=m)\n")
    got = {s.file for s in reach.candidates(tmp_path, [f])}
    assert got == {"wrapper.py", "direct.py", "alias.py", "unknown.py"}, got


def test_ambiguous_member_reference_is_capped_to_review(tmp_path):
    from aftershock import reach, verify
    from aftershock.contracts import Finding
    f = Finding(path="libs/r/src/pkg/schema.py", line=0, symbol="FieldSpec.description", message="Public object was removed")
    (tmp_path / "own.py").write_text("class _Doc:\n    description: str = ''\n\ndef f(doc):\n    return doc.description\n")
    (tmp_path / "lib.py").write_text("from pkg.schema import FieldSpec\nspec = FieldSpec()\nprint(spec.description)\n")
    spans = {s.file: s for s in reach.candidates(tmp_path, [f])}
    assert spans["own.py"].ambiguous and not spans["lib.py"].ambiguous
    sure = verify.Verdict(0.95, "breaks_consumer", 0.99, {}, 0)
    capped = verify.cap_ambiguous([spans["own.py"], spans["lib.py"]], [sure, sure])
    assert [v.status for v in capped] == ["needs_review", "breaks_consumer"]


def test_removed_parameter_passed_positionally_is_a_candidate_and_decided_by_rule(tmp_path):
    import subprocess
    from aftershock import reach, positional
    from aftershock.contracts import Finding
    lib = tmp_path / "lib"; (lib / "src/pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(lib)], check=True)
    (lib / "src/pkg/kb.py").write_text("def load(mode='basic', legacy_mode=None):\n    return mode\n")
    subprocess.run(["git", "-C", str(lib), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(lib), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "old"], check=True)
    f = Finding(path="src/pkg/kb.py", line=1, symbol="load(legacy_mode)", message="Parameter was removed")
    c = tmp_path / "consumer"; c.mkdir()
    (c / "two.py").write_text("from pkg.kb import load\nX = load('basic', 'r1')\n")
    (c / "one.py").write_text("from pkg.kb import load\nX = load('basic')\n")
    (c / "kw.py").write_text("from pkg.kb import load\nX = load(legacy_mode='r1')\n")
    (c / "bare.py").write_text("X = load('basic', 'r1')\n")
    spans = {s.file: s for s in reach.candidates(c, [f])}
    assert set(spans) == {"two.py", "one.py", "kw.py", "bare.py"}, set(spans)
    assert spans["two.py"].resolved and not spans["bare.py"].resolved
    rule = lambda s: positional.removed_param_breaks(s.code, s.finding, s.alias, str(lib), "HEAD")
    assert rule(spans["two.py"]) is True and rule(spans["one.py"]) is False and rule(spans["kw.py"]) is True


def test_same_file_constant_is_not_the_removed_library_object(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/web_api/utils/middleware.py", line=0, symbol="REQUEST_ID_HEADER", message="Public object was removed")
    (tmp_path / "own.py").write_text('REQUEST_ID_HEADER = "X-Request-ID"\nv = headers.get(REQUEST_ID_HEADER)\n')
    (tmp_path / "theirs.py").write_text("from web_api.utils.middleware import REQUEST_ID_HEADER\nv = headers.get(REQUEST_ID_HEADER)\n")
    got = {s.file for s in reach.candidates(tmp_path, [f])}
    assert got == {"theirs.py"}, got


def test_module_of_a_top_level_file_survives_the_prefix_strip():
    from aftershock import reach
    from aftershock.contracts import Finding
    assert Finding(path="job-runner/main.py", line=0, symbol="run", message="Public object was removed").module == "main"
    assert Finding(path="python/log-kit/src/logkit/tracing.py", line=0, symbol="x", message="m").module == "logkit.tracing"
    assert reach.module_of("job-runner/main.py") == "main" and reach.module_of("pkg/__init__.py") == "pkg"


def test_function_scope_binding_shadows_a_removed_module_object(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="utils/run_init.py", line=0, symbol="client", message="Public object was removed")
    (tmp_path / "local.py").write_text("import httpx\nasync def go():\n    async with httpx.AsyncClient() as client:\n        return await client.send(r)\n")
    (tmp_path / "param.py").write_text("def go(client):\n    return client.send(r)\n")
    (tmp_path / "real.py").write_text("from utils.run_init import client\ndef go():\n    return client.send(r)\n")
    (tmp_path / "bare.py").write_text("def go():\n    return client.send(r)\n")
    got = {s.file for s in reach.candidates(tmp_path, [f])}
    assert got == {"real.py", "bare.py"}, got


def test_keyword_on_a_tree_method_that_never_calls_the_library_is_skipped(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/gw/routers/orders.py", line=0, symbol="list_orders(limit)", message="Parameter was removed")
    (tmp_path / "base.py").write_text("class Base:\n    async def _neighbors(self, account_id, *, limit):\n        return await self.q(account_id, limit)\n")
    (tmp_path / "use.py").write_text("class A(Base):\n    async def go(self):\n        return await self._neighbors(u, limit=15)\n")
    (tmp_path / "wrap.py").write_text("from gw.routers import orders\nclass W:\n    def items(self, **kw):\n        return orders.list_orders(**kw)\n    def go(self):\n        return self.sites(limit=3)\n")
    got = {s.file for s in reach.candidates(tmp_path, [f])}
    assert got == {"wrap.py"}, got


def test_definition_is_not_a_use_and_a_resolved_removed_reference_is_decided_by_rule(tmp_path):
    from aftershock import reach, rules
    from aftershock.contracts import Finding
    f = Finding(path="src/pkg/conf.py", line=0, symbol="HOST", message="Public object was removed")
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/conf.py").write_text('HOST = "x"\nURL = HOST + "/api"\n')            # line 1 defines, line 2 uses
    (tmp_path / "use.py").write_text("from pkg.conf import HOST\nprint(HOST)\n")
    (tmp_path / "bare.py").write_text("print(HOST)\n")
    spans = {(s.file.split("/")[-1], s.line): s for s in reach.candidates(tmp_path, [f])}
    assert set(spans) == {("conf.py", 2), ("use.py", 1), ("use.py", 2), ("bare.py", 1)}, set(spans)   # use.py:1 = the import line
    assert rules.decide(spans[("conf.py", 2)], str(tmp_path), "a", "b") is rules.RULE_BREAK      # own module
    assert rules.decide(spans[("use.py", 2)], str(tmp_path), "a", "b") is rules.RULE_BREAK       # resolved import
    assert rules.decide(spans[("bare.py", 1)], str(tmp_path), "a", "b") is None                  # unresolvable: model


def test_module_import_style_reference_counts_as_resolved(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="httpx/_client.py", line=0, symbol="Client.__init__(app)", message="Parameter was removed")
    (tmp_path / "a.py").write_text("import httpx\nc = httpx.Client(app=x)\n")
    (tmp_path / "b.py").write_text("c = Client(app=x)\n")
    spans = {s.file: s for s in reach.candidates(tmp_path, [f])}
    assert spans["a.py"].resolved and not spans["b.py"].resolved


def test_module_scope_binding_inside_try_counts_as_the_files_own(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/pkg/log.py", line=0, symbol="logger", message="Public object was removed")
    (tmp_path / "own.py").write_text("import logging\ntry:\n    logger = logging.getLogger('x')\nexcept Exception:\n    logger = None\nlogger.info('hi')\n")
    (tmp_path / "theirs.py").write_text("from pkg.log import logger\nlogger.info('hi')\n")
    assert {s.file for s in reach.candidates(tmp_path, [f])} == {"theirs.py"}


def test_tuple_unpacking_and_loop_targets_shadow_a_removed_name(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    fs = [Finding(path="tool.py", line=0, symbol=n, message="Public object was removed") for n in ("node_map", "res2", "block_statements")]
    (tmp_path / "a.py").write_text("def f(items):\n    for node, (_, node_map) in items:\n        print(node_map)\n")
    (tmp_path / "b.py").write_text("res1, res2, res3 = 0, 0, 0\nprint(res2)\n")
    (tmp_path / "c.py").write_text("def g(rows):\n    block_statements, rest = rows\n    return block_statements[0]\n")
    (tmp_path / "d.py").write_text("from tool import node_map\nprint(node_map)\n")
    assert {s.file for s in reach.candidates(tmp_path, fs)} == {"d.py"}


def test_importing_a_removed_module_is_a_consequence_decided_by_rule(tmp_path):
    from aftershock import reach, rules
    from aftershock.contracts import Finding
    f = Finding(path="libs/t/src/pkg/providers/db_common.py", line=0, symbol="<module>", message="Public object was removed")
    (tmp_path / "libs/t/src/pkg/providers").mkdir(parents=True)
    (tmp_path / "libs/t/src/pkg/providers/handlers.py").write_text("from .db_common import TS_FIELD\nfrom . import db_common\nfrom .other import X\n")
    (tmp_path / "use.py").write_text("import pkg.providers.db_common\nfrom pkg.providers.db_common import TS_FIELD\n")
    spans = sorted((s.file.split("/")[-1], s.line) for s in reach.candidates(tmp_path, [f]))
    assert spans == [("handlers.py", 1), ("handlers.py", 2), ("use.py", 1), ("use.py", 2)], spans
    assert all(rules.decide(s, str(tmp_path), "a", "b") is rules.RULE_BREAK for s in reach.candidates(tmp_path, [f]))


def test_route_contract_finds_removed_paths_in_a_typescript_consumer(tmp_path):
    import subprocess
    from aftershock import routes
    lib = tmp_path / "lib"; (lib / "svc/routers").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(lib)], check=True)
    (lib / "svc/routers/orders.py").write_text('from fastapi import APIRouter\nrouter = APIRouter(prefix="/v1")\n@router.get("/items/{item_id}")\nasync def site(item_id: str): ...\n@router.post("/sites")\nasync def create(): ...\n')
    g = lambda *a: subprocess.run(["git", "-C", str(lib), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    g("add", "-A"); g("commit", "-q", "-m", "old"); old = subprocess.run(["git", "-C", str(lib), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (lib / "svc/routers/orders.py").write_text('from fastapi import APIRouter\nrouter = APIRouter(prefix="/v1")\n@router.get("/item/{item_id}")\nasync def site(item_id: str): ...\n@router.post("/sites")\nasync def create(): ...\n')
    g("add", "-A"); g("commit", "-q", "-m", "new")
    d = routes.diff(routes.routes(str(lib), old), routes.routes(str(lib), "HEAD"))
    assert [r.path for r in d["removed"]] == ["/v1/items/{item_id}"] and [r.path for r in d["added"]] == ["/v1/item/{item_id}"]
    ui = tmp_path / "ui/src"; ui.mkdir(parents=True)
    (ui / "api.ts").write_text("const a = fetch(`/v1/items/${id}`);\nconst b = fetch('/v1/items');\nconst c = fetch('/v1/items/' + id + '/stats');\n")
    hits = routes.consumers(tmp_path / "ui", ["/v1/items/{item_id}"])
    assert [ln for _, ln, _ in hits["/v1/items/{item_id}"]] == [1]        # line 3 builds the path from pieces: the documented ceiling


def test_settings_keys_follow_env_prefix_and_aliases():
    from aftershock.config_contract import settings_keys
    src = ("from pydantic_settings import BaseSettings, SettingsConfigDict\nfrom pydantic import Field\n"
           "class S(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix='APP_GW_')\n"
           "    auth_url: str = 'x'\n    redis_url: str = Field(default='r', alias='APP_REDIS_URL')\n    _private: int = 0\n"
           "class Old:\n    class Config:\n        env_prefix = 'AR_'\n    TIMEOUT: int = 3\n")
    assert settings_keys(src) == {"APP_GW_AUTH_URL", "APP_REDIS_URL", "TIMEOUT"}


def test_added_required_on_init_matches_the_class_call(tmp_path):
    import subprocess
    from aftershock import positional
    from aftershock.contracts import Finding
    lib = tmp_path / "lib"; (lib / "src/pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(lib)], check=True)
    (lib / "src/pkg/cap.py").write_text("class Cap:\n    def __init__(self, assistant_text):\n        pass\n")
    subprocess.run(["git", "-C", str(lib), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(lib), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "new"], check=True)
    f = Finding(path="src/pkg/cap.py", line=2, symbol="Cap.__init__(assistant_text)", message="Parameter was added as required")
    assert positional.added_required_breaks("capture = Cap()", f, "", "", str(lib), "HEAD") is True
    assert positional.added_required_breaks("capture = Cap('x')", f, "", "", str(lib), "HEAD") is False


def test_route_paths_include_router_prefixes_from_other_files(tmp_path):
    import subprocess
    from aftershock import routes
    lib = tmp_path / "lib"; (lib / "src/svc/routes").mkdir(parents=True)
    (lib / "src/svc/routes/__init__.py").write_text("")
    (lib / "src/svc/routes/audit.py").write_text('from fastapi import APIRouter\nrouter = APIRouter()\n@router.get("/static/latest")\ndef latest(): ...\n')
    (lib / "src/svc/api.py").write_text('from fastapi import APIRouter\nfrom routes import audit\napi = APIRouter()\napi.include_router(audit.router, prefix="/reports")\n')   # service dir on sys.path
    (lib / "src/svc/main.py").write_text('from fastapi import FastAPI\nfrom .api import api\napp = FastAPI()\napp.include_router(api, prefix="/api")\n')
    subprocess.run(["git", "init", "-q", str(lib)], check=True); subprocess.run(["git", "-C", str(lib), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(lib), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], check=True)
    assert [r.path for r in routes.routes(str(lib), "HEAD")] == ["/api/reports/static/latest"]


def test_estate_routes_resolve_a_mount_across_repositories(tmp_path):
    import subprocess
    from aftershock import routes
    def repo(name, files):
        r = tmp_path / name
        for f, body in files.items():
            (r / f).parent.mkdir(parents=True, exist_ok=True); (r / f).write_text(body)
        subprocess.run(["git", "init", "-q", str(r)], check=True); subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], check=True)
        return str(r)
    lib = repo("utils", {"python/voice/src/voicekit/fastapi.py": 'from fastapi import APIRouter\ndef create_voice_router(*, settings):\n    router = APIRouter()\n    @router.get("/token")\n    def token(): ...\n    return router\n'})
    svc = repo("chat", {"services/chat/src/chat/voice.py": 'from voicekit.fastapi import create_voice_router\nrouter = create_voice_router(settings=None)\n',
                       "services/chat/src/chat/main.py": 'from fastapi import FastAPI\nfrom .voice import router as voice_router\napp = FastAPI()\napp.include_router(voice_router, prefix="/v1/voice")\n'})
    svc2 = repo("store", {"api/routes/voice.py": 'from voicekit.fastapi import create_voice_router\nrouter = create_voice_router(settings=None)\n',
                           "api/main.py": 'from fastapi import FastAPI\nfrom routes import voice\napp = FastAPI()\napp.include_router(voice.router, prefix="/api/voice")\n'})
    assert [r.path for r in routes.routes(lib, "HEAD")] == ["/token"]                          # alone: unmounted
    assert sorted(r.path for r in routes.estate_routes([(lib, "HEAD"), (svc, "HEAD"), (svc2, "HEAD")])) == ["/api/voice/token", "/v1/voice/token"]


def test_consumer_flags_a_map_entry_nothing_references(tmp_path):
    from aftershock import routes
    ui = tmp_path / "ui/src"; ui.mkdir(parents=True)
    (ui / "api.ts").write_text('export const E = {\n  signIn: "/auth/login",\n  items: "/v1/items",\n};\n')
    (ui / "use.ts").write_text('import { E } from "./api";\nfetch(E.items);\n')
    hits = routes.consumers(tmp_path / "ui", ["/auth/login", "/v1/items"])
    assert "[stale constant" in hits["/auth/login"][0][2] and "[stale constant" not in hits["/v1/items"][0][2]


def test_a_function_named_inside_depends_is_not_a_consumer_call(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/svc/deps.py", line=0, symbol="get_service(lockout)", message="Parameter was added as required")
    (tmp_path / "a.py").write_text("from fastapi import Depends\nfrom svc.deps import get_service\ndef route(s=Depends(get_service)):\n    return s\nx = get_service()\n")
    assert [s.line for s in reach.candidates(tmp_path, [f])] == [5]


def test_the_import_line_of_a_removed_object_is_a_consequence(tmp_path):
    from aftershock import reach, rules
    from aftershock.contracts import Finding
    f = Finding(path="src/pkg/prompts/ctx.py", line=0, symbol="build_ctx", message="Public object was removed")
    (tmp_path / "src/pkg/agent").mkdir(parents=True)
    (tmp_path / "src/pkg/agent/a.py").write_text("from ..prompts.ctx import build_ctx\nx = build_ctx()\n")
    spans = sorted((s.line, s.resolved) for s in reach.candidates(tmp_path, [f]))
    assert spans == [(1, True), (2, True)], spans
    assert all(rules.decide(s, str(tmp_path), "a", "b") is rules.RULE_BREAK for s in reach.candidates(tmp_path, [f]))


def test_member_on_another_imported_class_of_the_same_package_is_not_a_candidate(tmp_path):
    from aftershock import reach
    from aftershock.contracts import Finding
    f = Finding(path="src/svc/services/billing_client.py", line=0, symbol="Account.quota", message="Public object was removed")
    (tmp_path / "src/svc/repositories").mkdir(parents=True)
    (tmp_path / "src/svc/repositories/plan.py").write_text("from ..models import Plan\nfrom ..services.billing_client import Account\ncols = (Plan.quota,)\nx = Account.quota\n")
    assert [s.line for s in reach.candidates(tmp_path, [f])] == [4]


def test_settings_defaults_reads_new_keys_and_changed_defaults():
    from aftershock.config_contract import settings_defaults
    old = "from pydantic_settings import BaseSettings, SettingsConfigDict\nfrom pydantic import Field\nclass S(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix='AR_ADAPTER_')\n    db_connect_timeout_sec: int = Field(default=30)\n"
    new = old + "    db_query_timeout_sec: int = Field(default=900, description='x')\n    db_connect_timeout_sec: int = Field(default=45)\n"
    od, nd = settings_defaults(old), settings_defaults(new)
    assert od == {"AR_ADAPTER_DB_CONNECT_TIMEOUT_SEC": "30"}
    assert nd["AR_ADAPTER_DB_QUERY_TIMEOUT_SEC"] == "900" and nd["AR_ADAPTER_DB_CONNECT_TIMEOUT_SEC"] == "45"


def _fake_cie(tmp_path, body: str) -> str:
    p = tmp_path / "fake-aftershock"
    p.write_text("#!/usr/bin/env python3\nimport sys, json\nout = sys.argv[sys.argv.index('--json') + 1]\n" + body)
    p.chmod(0o755)
    return str(p)


def test_pr_check_api_impact_crash_is_not_a_clean_result(tmp_path):
    """A crashed `aftershock api-impact` leaves no JSON; it used to render as '✅ no caller affected'."""
    import pytest
    from aftershock import prcheck
    crash = _fake_cie(tmp_path, "sys.stderr.write('Traceback...\\nValueError: boom\\n'); sys.exit(1)\n")
    with pytest.raises(prcheck.CheckFailed, match="exited 1: ValueError: boom"):
        prcheck.api_impact(crash, "lib", "pkg", "src", "a", "b", str(tmp_path), 0.02)
    silent = _fake_cie(tmp_path, "open(out, 'w').close()\n")          # exit 0 but an empty result file
    with pytest.raises(prcheck.CheckFailed):
        prcheck.api_impact(silent, "lib", "pkg", "src", "a", "b", str(tmp_path), 0.02)
    missing = _fake_cie(tmp_path, "pass\n")                          # exit 0 and no file at all
    with pytest.raises(prcheck.CheckFailed):
        prcheck.api_impact(missing, "lib", "pkg", "src", "a", "b", str(tmp_path), 0.02)


def test_pr_check_api_impact_success_still_parses(tmp_path):
    from aftershock import prcheck
    ok = _fake_cie(tmp_path, "print('Contract diff pkg a -> b: 3 findings'); json.dump([{'status': 'compatible'}], open(out, 'w'))\n")
    findings, rows, _ = prcheck.api_impact(ok, "lib", "pkg", "src", "a", "b", str(tmp_path), 0.02)
    assert findings == 3 and rows == [{"status": "compatible"}]
    none = _fake_cie(tmp_path, "print('Contract diff pkg a -> b: 0 findings'); json.dump([], open(out, 'w'))\n")
    assert prcheck.api_impact(none, "lib", "pkg", "src", "a", "b", str(tmp_path), 0.02)[1] == []


def test_config_contract_helper_prefix_settings_subclass_and_alias_choices(tmp_path):
    """Public-estate shapes (Prefect): env_prefix built by a helper from a path tuple, a base class named
    `...BaseSettings`, and legacy names listed in AliasChoices."""
    (tmp_path / "base.py").write_text(textwrap.dedent('''
        from pydantic_settings import BaseSettings
        class PrefectBaseSettings(BaseSettings): ...
        def build_settings_config(path: tuple[str, ...] = tuple()):
            env_prefix = f"PREFECT_{'_'.join(path).upper()}_" if path else "PREFECT_"
            return dict(env_prefix=env_prefix)
    '''))
    (tmp_path / "api.py").write_text(textwrap.dedent('''
        from pydantic import AliasChoices, Field
        from base import PrefectBaseSettings, build_settings_config
        class ServerAPISettings(PrefectBaseSettings):
            model_config = build_settings_config(("server", "api"))
            host: str = Field(default="127.0.0.1")
            auth: str
            ui: bool = Field(default=True, validation_alias=AliasChoices("prefect_server_api_ui", "prefect_ui_enabled"))
    '''))
    c = config_contract.scan(tmp_path, [])
    assert {"PREFECT_SERVER_API_HOST", "PREFECT_SERVER_API_AUTH", "PREFECT_SERVER_API_UI", "PREFECT_UI_ENABLED"} <= c.read_by_code
    assert "PREFECT_SERVER_API_AUTH" in c.required_by_code and "PREFECT_SERVER_API_HOST" not in c.required_by_code
    helpers = config_contract.collect_helpers(tmp_path)
    d = config_contract.settings_defaults((tmp_path / "api.py").read_text(), helpers)
    assert d["PREFECT_SERVER_API_HOST"] == "'127.0.0.1'" and d["PREFECT_UI_ENABLED"] == "True"
    assert config_contract.settings_defaults((tmp_path / "api.py").read_text()) != d      # without the helper the prefix is unknown


def test_verify_without_api_key_leaves_undecided_spans_for_review(monkeypatch, capsys):
    """No key, no model: spans the rules could not decide are SKIPPED (needs_review), never judged and never a crash."""
    from aftershock import verify
    monkeypatch.setattr(verify, "TypeSafeClient", object)        # the [model] extra is installed...
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)        # ...but no key
    out = verify.verify([{"x": 1}, {"x": 2}])
    assert out == [verify.SKIPPED, verify.SKIPPED] and out[0].status == "needs_review"
    assert "TYPESAFE_API_KEY is not set" in capsys.readouterr().err
    assert verify.verify([]) == []


def _span(finding, code="x", receiver="", ambiguous=False, resolved=True):
    return reach.Span("m.py", 1, code, code, finding, receiver, "", ambiguous, resolved)


def test_removed_member_in_its_own_module_is_not_a_break_by_name(tmp_path):
    """pydantic/types.py defines decode/encode/__hash__ on many classes; removing EncodedStr.decode must not 'verify'
    every other class's method of that name. The consumer's own module resolves module-level objects only."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "types.py").write_text(textwrap.dedent("""
        class Other:
            def decode(self, data): ...
        class Encoded(Base):
            def decode(self, data): ...
    """))
    f = contracts.Finding("pkg/types.py", 1, "EncodedStr.decode", "Public object was removed")
    spans = [s for s in reach.candidates(tmp_path, [f]) if s.finding is f]
    assert spans == []        # was a 'verified' break on every one; a def named like a removed member is never a use


def test_member_through_ambiguous_receiver_is_never_decided_by_rule():
    from aftershock import rules
    f = contracts.Finding("pkg/m.py", 1, "Field.to_argument(typed)", "Parameter was added as required")
    s = _span(f, "field.to_argument(info)", receiver="field", ambiguous=True)
    assert rules.decide(s, "/nonexistent", "a", "b") is None


def test_removed_name_still_bound_in_new_module_is_review_not_break(tmp_path):
    """griffe sees one branch of `if TYPE_CHECKING: X = ... else: class X`; the name is not verified as removed."""
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "net.py").write_text("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    IPv = int\nelse:\n    class IPv: ...\n")
    (tmp_path / "pkg" / "gone.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "__init__.py").write_text("__all__ = ('Lazy',)\n_dynamic_imports = {'Lazy': (__spec__.parent, '.net')}\n")
    run("add", "."); run("commit", "-q", "-m", "c")
    from aftershock import rules
    still = contracts.Finding("pkg/net.py", 1, "IPv", "Public object was removed")
    real = contracts.Finding("pkg/net.py", 1, "Missing", "Public object was removed")
    lazy = contracts.Finding("pkg/__init__.py", 1, "Lazy", "Public object was removed")
    assert rules.still_bound(_span(lazy), str(tmp_path), "HEAD") is False          # exported by name through __all__ / lazy table
    assert rules.still_bound(_span(still), str(tmp_path), "HEAD") is False
    assert rules.still_bound(_span(real), str(tmp_path), "HEAD") is True
    assert rules.decide(_span(still, "pkg.IPv"), str(tmp_path), "HEAD", "HEAD").status == "needs_review"   # never handed to the model
    assert rules.decide(_span(real, "pkg.Missing"), str(tmp_path), "HEAD", "HEAD") is rules.RULE_BREAK


def test_same_named_object_of_another_module_is_not_a_candidate(tmp_path):
    """Prefect e977a2ca: a bare `serve(...)` imported from prefect.task_worker, and `prefect.context.X.get()`, are not
    the changed prefect.cli.task.serve / prefect.cli.cloud.webhook.get."""
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        import prefect
        from prefect.task_worker import serve
        from prefect.cli.task import serve as cli_serve
        from prefect import tasks
        def a(self):
            serve(self)
            cli_serve(1, 2)
            prefect.cli.task.serve(1, 2)
            prefect.context.SyncClientContext.get()
            prefect.cli.cloud.webhook.get(1)
    """))
    kind = contracts.Finding("src/prefect/cli/task.py", 1, "serve(module)", "Parameter kind was changed: positional or keyword -> keyword-only")
    gone = contracts.Finding("src/prefect/cli/cloud/webhook.py", 1, "get", "Public object was removed")
    lines = {(s.finding.symbol, s.line) for s in reach.candidates(tmp_path, [kind, gone])}
    assert ("serve(module)", 8) in lines and ("serve(module)", 9) in lines       # the real ones (imported alias, full path)
    assert ("serve(module)", 7) not in lines                                     # prefect.task_worker.serve
    assert ("get", 11) in lines and ("get", 10) not in lines                     # SyncClientContext.get is another object


def test_module_receiver_and_own_same_named_def_are_not_candidates(tmp_path):
    """Prefect: `prefect.context.X` is a module, not an instance of TaskRunResponse (26 false 'removed field' matches);
    a file's own `def build_image` is not the library's changed build_image."""
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        import prefect
        from prefect.config import settings
        def build_image(arch): ...
        def a():
            prefect.context.FlowRunContext.model_rebuild()
            settings.context
            build_image("x")
    """))
    field = contracts.Finding("src/prefect/server/schemas/responses.py", 1, "TaskRunResponse.context", "Public object was removed")
    func = contracts.Finding("src/prefect/cli/dev.py", 1, "build_image(arch)", "Parameter kind was changed: positional or keyword -> keyword-only")
    mods = {"prefect", "prefect.context", "prefect.cli", "prefect.cli.dev"}
    lines = {(s.finding.object_name, s.line) for s in reach.candidates(tmp_path, [field, func], modules=mods)}
    assert ("TaskRunResponse.context", 6) not in lines      # receiver `prefect` is a module
    assert ("TaskRunResponse.context", 7) in lines          # a module-level instance (`settings`) may be an instance: still a candidate
    assert ("build_image", 8) not in lines                  # defined in this very file
    assert ("TaskRunResponse.context", 6) in {(s.finding.object_name, s.line) for s in reach.candidates(tmp_path, [field])}   # without the library's module list nothing changes


def test_removed_member_through_a_library_variable_is_not_decided_by_rule(tmp_path):
    """Flask daca74d9: `_cv_app.get(None)` (a ContextVar of the library) was 'verified' as a use of the removed
    SecureCookieSession.get. Only a receiver that IS the class resolves a removed member."""
    (tmp_path / "flask").mkdir()
    (tmp_path / "flask" / "c.py").write_text(textwrap.dedent("""
        from .globals import _cv_app
        from .sessions import SecureCookieSession
        def a(s):
            _cv_app.get(None)
            SecureCookieSession.get(s, "k")
    """))
    f = contracts.Finding("src/flask/sessions.py", 1, "SecureCookieSession.get", "Public object was removed")
    from aftershock import rules
    by_line = {s.line: s for s in reach.candidates(tmp_path, [f])}
    assert 5 in by_line and not by_line[5].resolved and rules.decide(by_line[5], "/x", "a", "b") is None
    assert by_line[6].resolved and rules.decide(by_line[6], "/x", "a", "b") is rules.RULE_BREAK


def test_settings_module_detection_covers_directories():
    """Prefect keeps its settings in settings/models/root.py: the file name alone says nothing."""
    from aftershock.prcheck import SETTINGS_PATH as S
    assert all(S.search(f) for f in ["src/prefect/settings/models/root.py", "jobs/config.py", "svc/server/settings.py"])
    assert not any(S.search(f) for f in ["src/prefect/flows.py", "docs/configuration.md"])


def _two_commit_lib(tmp_path):
    """pkg at A: pkg/mod.py defines old_fn(a, b). At B: old_fn is gone, pkg/new.py defines new_fn(a, b)."""
    import subprocess
    lib = tmp_path / "lib"; (lib / "pkg").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(lib), *a], check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (lib / "pkg" / "__init__.py").write_text("")
    (lib / "pkg" / "mod.py").write_text("def old_fn(a, b):\n    return a + b\n")
    run("add", "."); run("commit", "-q", "-m", "A"); a = run("rev-parse", "HEAD")
    (lib / "pkg" / "mod.py").write_text("X = 1\n")
    (lib / "pkg" / "new.py").write_text("def new_fn(a, b):\n    return a + b\n")
    run("add", "."); run("commit", "-q", "-m", "B"); b = run("rev-parse", "HEAD")
    return str(lib), a, b


def test_fixcheck_ladder_good_fix_bad_fix_and_behaviour_change(tmp_path):
    from aftershock import fixcheck
    lib, a, b = _two_commit_lib(tmp_path)
    cons = tmp_path / "cons"; (cons / "app").mkdir(parents=True)
    (cons / "app" / "__init__.py").write_text("")
    src = "from pkg.mod import old_fn\n\nRESULT = old_fn(1, 2)\n"
    (cons / "app" / "main.py").write_text(src)
    f = contracts.Finding("pkg/mod.py", 1, "old_fn", "Public object was removed")
    args = dict(target=f, findings=[f], lib_root=lib, package="pkg", old_ref=a, new_ref=b, consumer_root=str(cons))

    good = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 1, 1, "from pkg.new import new_fn as old_fn")], **args)
    assert [r.status for r in good.rungs] == ["pass", "pass", "pass", "pass"] and good.level == "runs", good.summary()
    assert not good.behaviour_change

    wrong = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 1, 1, "from pkg.mod import new_fn as old_fn")], **args)
    assert wrong.level == "parses" and "pkg.mod has no `new_fn`" in wrong.rungs[1].detail       # stops at resolves

    broken = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 1, 1, "from pkg.new import (")], **args)
    assert broken.level == "fails"

    unfixed = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 3, 3, "RESULT = old_fn(1, 2)  # todo")], **args)
    assert unfixed.level == "parses" and "has no `old_fn`" in unfixed.rungs[1].detail           # the edited line still names it

    partial = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 3, 3, "RESULT = 3")], **args)
    assert partial.rungs[2].status == "fail" and "still breaks at line 1" in partial.rungs[2].detail   # the import is left

    gutted = fixcheck.check(src, "app/main.py", [fixcheck.Edit("app/main.py", 1, 1, ""),
                                                 fixcheck.Edit("app/main.py", 3, 3, "RESULT = 3")], **args)
    assert gutted.level == "runs" and "deletes `from pkg.mod import old_fn`" in gutted.behaviour_change   # runs, yet flagged


def test_fixcheck_missing_dependency_is_not_a_failed_fix(tmp_path):
    from aftershock import fixcheck
    lib, a, b = _two_commit_lib(tmp_path)
    cons = tmp_path / "cons"; cons.mkdir()
    src = "import some_absent_dependency\nfrom pkg.mod import old_fn\n"
    (cons / "tool.py").write_text(src)
    f = contracts.Finding("pkg/mod.py", 1, "old_fn", "Public object was removed")
    fc = fixcheck.check(src, "tool.py", [fixcheck.Edit("tool.py", 2, 2, "from pkg.new import new_fn as old_fn")], target=f,
                        findings=[f], lib_root=lib, package="pkg", old_ref=a, new_ref=b, consumer_root=str(cons))
    assert fc.rungs[-1].status == "not_run" and "some_absent_dependency" in fc.rungs[-1].detail and fc.level == "clears"


def test_fixcheck_apply_tracks_ranges_after_size_changes():
    from aftershock import fixcheck
    src = "a\nb\nc\nd\n"
    new, ranges = fixcheck.apply(src, [fixcheck.Edit("f", 1, 1, "a1\na2"), fixcheck.Edit("f", 3, 3, "c1")])
    assert new == "a1\na2\nb\nc1\nd\n" and ranges == [(1, 2), (4, 4)]


def test_fixcheck_in_repo_file_importing_a_dropped_dependency_does_not_resolve(tmp_path):
    """Prefect #20838 dropped typer; its own script still imported it, so no import-path fix could hold."""
    import subprocess
    from aftershock import fixcheck
    lib = tmp_path / "lib"; (lib / "pkg").mkdir(parents=True); (lib / "scripts").mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(lib), *a], check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (lib / "pyproject.toml").write_text('[project]\nname = "pkg"\ndependencies = ["typer>=0.9", "Rich"]\n')
    (lib / "pkg" / "__init__.py").write_text(""); (lib / "pkg" / "root.py").write_text("app = 1\n")
    src = "import typer\nfrom pkg.root import app\n"
    (lib / "scripts" / "docs.py").write_text(src)
    run("add", "."); run("commit", "-q", "-m", "A"); a = run("rev-parse", "HEAD")
    (lib / "pyproject.toml").write_text('[project]\nname = "pkg"\ndependencies = ["rich"]\n')
    (lib / "pkg" / "root.py").unlink(); (lib / "pkg" / "_app.py").write_text("app = 2\n")
    run("add", "-A"); run("commit", "-q", "-m", "B"); b = run("rev-parse", "HEAD")
    f = contracts.Finding("pkg/root.py", 1, "<module>", "Public object was removed")
    fc = fixcheck.check(src, "scripts/docs.py", [fixcheck.Edit("scripts/docs.py", 2, 2, "from pkg._app import app")], target=f,
                        findings=[f], lib_root=str(lib), package="pkg", old_ref=a, new_ref=b)
    assert fc.level == "parses" and "imports `typer`, which pkg no longer depends on" in fc.rungs[1].detail
    assert fixcheck.declared_dependencies(str(lib), a) == {"typer", "rich"}


def _lib(tmp_path, v1: dict, v2: dict):
    """A library repo with two commits; v1/v2 map paths to contents (None deletes)."""
    import subprocess
    lib = tmp_path / "lib"; lib.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(lib), *a], check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    refs = []
    for files in (v1, v2):
        for p, txt in files.items():
            fp = lib / p
            if txt is None:
                fp.unlink()
            else:
                fp.parent.mkdir(parents=True, exist_ok=True); fp.write_text(txt)
        run("add", "-A"); run("commit", "-q", "-m", "c"); refs.append(run("rev-parse", "HEAD"))
    return str(lib), refs[0], refs[1]


def _span_for(root, finding, line):
    return next(s for s in reach.candidates(root, [finding]) if s.line == line)


def test_fix_keyword_rewrite_holds_up(tmp_path):
    from aftershock import fix, fixcheck
    lib, a, b = _lib(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": "def f(a, b, c=0):\n    return a + b + c\n"},
                     {"pkg/m.py": "def f(*, a, b, c=0):\n    return a + b + c\n"})
    cons = tmp_path / "cons"; cons.mkdir()
    src = "from pkg.m import f\nX = f(1, 2, c=3)\n"
    (cons / "app.py").write_text(src)
    fd = contracts.Finding("pkg/m.py", 1, "f(a)", "Parameter kind was changed: positional or keyword -> keyword-only")
    fx = fix.propose(_span_for(cons, fd, 2), src, lib, "pkg", None, a, b)
    assert fx and fx[0].method == "keyword" and fx[0].exact and fx[0].edits[0].text == "X = f(a=1, b=2, c=3)"
    fc = fixcheck.check(src, "app.py", fx[0].edits, target=fd, findings=[fd], lib_root=lib, package="pkg", old_ref=a,
                        new_ref=b, consumer_root=str(cons))
    assert fc.level == "runs" and not fc.behaviour_change, fc.summary()


def test_fix_moved_import_and_changed_definition(tmp_path):
    from aftershock import fix, fixcheck
    lib, a, b = _lib(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": "def helper(x):\n    return x\n\ndef other():\n    pass\n"},
                     {"pkg/m.py": "def other():\n    pass\n", "pkg/util.py": "def helper(x):\n    return x\n"})
    cons = tmp_path / "cons"; cons.mkdir()
    src = "from pkg.m import helper, other\nY = helper(1)\n"
    (cons / "app.py").write_text(src)
    fd = contracts.Finding("pkg/m.py", 1, "helper", "Public object was removed")
    fx = fix.propose(_span_for(cons, fd, 2), src, lib, "pkg", None, a, b)
    mv = next(x for x in fx if x.method == "moved")
    assert mv.exact and mv.edits[0].text == "from pkg.m import other\nfrom pkg.util import helper"
    fc = fixcheck.check(src, "app.py", mv.edits, target=fd, findings=[fd], lib_root=lib, package="pkg", old_ref=a,
                        new_ref=b, consumer_root=str(cons))
    assert fc.level == "runs", fc.summary()
    # same name, different body at the new home: still proposed, but never called exact
    lib2, a2, b2 = _lib(tmp_path / "x", {"pkg/__init__.py": "", "pkg/m.py": "def helper(x):\n    return x\n"},
                        {"pkg/m.py": "Z = 1\n", "pkg/util.py": "def helper(x):\n    return -x\n"})
    mv2 = fix.moved(_span_for(cons, fd, 2), src, lib2, "pkg", None, a2, b2)
    assert mv2 and not mv2.exact and "definition changed" in mv2.note


def test_fix_by_analogy_from_the_change_diff(tmp_path):
    from aftershock import fix
    diff = ("@@ -1,2 +1,2 @@\n-from pkg.root import app\n+from pkg._app import app\n"
            "@@ -9,2 +9,2 @@\n-    from pkg.root import app, is_interactive\n+    from pkg._app import app, is_interactive\n")
    assert fix.substitutions(diff, "pkg.root").most_common(1)[0] == (("pkg.root", "pkg._app"), 2)
    cons = tmp_path / "cons"; cons.mkdir()
    src = "def main():\n    from pkg.root import app\n    app()\n"
    (cons / "tool.py").write_text(src)
    fd = contracts.Finding("pkg/root.py", 1, "<module>", "Public object was removed")
    fx = fix.analogy(_span_for(cons, fd, 2), src, diff)
    assert fx and fx.edits[0].text == "    from pkg._app import app" and fx.extra["count"] == 2


def test_fix_analogy_skips_half_of_a_multiline_edit():
    from aftershock import fix
    diff = "@@ -1 +1,3 @@\n-from pkg.root import app\n+from pkg._utilities import (\n+    app,\n+)\n"
    assert not fix.substitutions(diff, "pkg.root")


def test_member_moved_to_a_base_class_is_not_a_verified_break(tmp_path):
    """flask 1d8b53f7: Flask's members moved into sansio App; griffe reports them removed from Flask."""
    from aftershock import rules
    lib, a, b = _lib(tmp_path, {"pkg/__init__.py": "", "pkg/app.py": "class Flask:\n    jinja_options = {}\n    def run(self):\n        return self.jinja_options\n"},
                     {"pkg/app.py": "from .sansio.app import App\nclass Flask(App):\n    def run(self):\n        return self.jinja_options\n",
                      "pkg/sansio/__init__.py": "", "pkg/sansio/app.py": "class App:\n    jinja_options = {}\n"})
    f = contracts.Finding("pkg/app.py", 1, "Flask.jinja_options", "Public object was removed")
    gone = contracts.Finding("pkg/app.py", 1, "Flask.vanished", "Public object was removed")
    assert rules.inherited(f, lib, b) and not rules.inherited(gone, lib, b)
    s = _span(f, "return self.jinja_options", receiver="self")
    assert rules.decide(s, lib, a, b).status == "needs_review"
    assert rules.decide(_span(gone, "self.vanished", receiver="self"), lib, a, b) is rules.RULE_BREAK


def test_attribute_writes_are_not_uses_but_augmented_assignments_are(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("class C:\n    def __init__(self, kw):\n        self.name = kw['name']\n        self.name += '!'\n        print(self.name)\n")
    f = contracts.Finding("pkg/m.py", 1, "C.name", "Public object was removed")
    assert sorted(s.line for s in reach.candidates(tmp_path, [f])) == [4, 5]


def test_keyword_on_an_unrelated_callee_is_ambiguous(tmp_path):
    """pandas 1a561f8f: `plot(..., kind=kind)` is not WrappedCythonOp(kind=); still a candidate, never auto-verified."""
    (tmp_path / "c.py").write_text("def f(backend, op, data):\n    backend.plot(data, kind='line')\n    WrappedCythonOp(kind='x', how='sum')\n")
    f = contracts.Finding("pandas/core/groupby/ops.py", 1, "WrappedCythonOp.__init__(kind)", "Parameter was removed")
    by_line = {s.line: s for s in reach.candidates(tmp_path, [f])}
    assert by_line[2].ambiguous and not by_line[3].ambiguous


def test_verify_survives_a_dropped_connection(capsys):
    import pytest
    pytest.importorskip("typesafe_sdk")
    """One DNS failure mid-run used to crash a benchmark. Transient errors are review items."""
    from aftershock import verify

    class Flaky:
        calls = 0
        def system_one(self, **kw):
            Flaky.calls += 1
            raise verify.TypeSafeAPIConnectionError("Connection error")
        def close(self): ...
    out = verify.verify([{"i": i} for i in range(5)], client=Flaky())
    assert out == [verify.SKIPPED] * 5 and Flaky.calls == 3            # stops calling after three in a row
    assert "5 model call(s) failed" in capsys.readouterr().err


def test_fixcheck_flags_private_library_names_in_a_consumer_fix(tmp_path):
    from aftershock import fixcheck
    lib, a, b = _lib(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": "def helper():\n    pass\n"},
                     {"pkg/m.py": "def _helper():\n    pass\n"})
    cons = tmp_path / "cons"; cons.mkdir()
    src = "from pkg.m import helper\nhelper()\n"
    (cons / "app.py").write_text(src)
    f = contracts.Finding("pkg/m.py", 1, "helper", "Public object was removed")
    fc = fixcheck.check(src, "app.py", [fixcheck.Edit("app.py", 1, 1, "from pkg.m import _helper as helper")], target=f, findings=[f],
                        lib_root=lib, package="pkg", old_ref=a, new_ref=b, consumer_root=str(cons))
    assert fc.level == "runs" and fc.private == "pkg.m._helper" and "PRIVATE API" in fc.summary()


def test_pr_packages_any_depth_and_stub_files(tmp_path):
    """pydantic #12924 changed pydantic-core/python/pydantic_core/*.pyi; a two-levels-deep scan never saw the package."""
    from aftershock import prcheck
    files = {"pydantic/__init__.py": "", "pydantic/main.py": "x = 1\n",
             "pydantic-core/python/pydantic_core/__init__.py": "", "pydantic-core/python/pydantic_core/_pydantic_core/__init__.pyi": "",
             "src/click/__init__.py": "", "src/click/core.py": "", "scripts/tool.py": "", "tests/test_a.py": ""}
    edit = {k: v + "# changed\n" for k, v in files.items() if k != "pydantic/__init__.py" and not k.endswith("core/__init__.py")}
    lib, a, b = _lib(tmp_path, files, edit)
    assert prcheck.packages(lib, a, b) == [("", "pydantic"), ("pydantic-core/python", "pydantic_core"), ("src", "click")]


def test_member_of_a_class_with_an_external_base_is_not_verified_removed(tmp_path):
    """fastapi #16236 removed APIRouter.app because starlette's Router (an external base) provides it."""
    from aftershock import rules
    lib, a, b = _lib(tmp_path, {"fastapi/__init__.py": "", "fastapi/routing.py": "from starlette import routing\nclass APIRouter(routing.Router):\n    async def app(self, scope):\n        pass\n"},
                     {"fastapi/routing.py": "from starlette import routing\nclass APIRouter(routing.Router):\n    pass\n"})
    f = contracts.Finding("fastapi/routing.py", 1, "APIRouter.app", "Public object was removed")
    assert rules.inherited(f, lib, b)
    assert rules.decide(_span(f, "await self.app(scope)", receiver="self"), lib, a, b).status == "needs_review"


def test_instance_attributes_make_a_member_ambiguous(tmp_path):
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        class APIRoute:
            def __init__(self):
                self.app = None
        def f(route):
            return route.app
    """))
    fd = contracts.Finding("fastapi/routing.py", 1, "APIRouter.app", "Public object was removed")
    s = [x for x in reach.candidates(tmp_path, [fd]) if x.line == 6][0]
    assert s.ambiguous


def test_signature_reader_skips_overload_stubs(tmp_path):
    """click's progressbar: a keyword-only @overload ahead of the implementation was read as the signature, so every
    positional call 'broke' (still-broken-at-HEAD scan, examples/termui/termui.py)."""
    from aftershock import positional
    src = textwrap.dedent("""
        import typing as t
        @t.overload
        def progressbar(*, length: int, label: str = None): ...
        @t.overload
        def progressbar(iterable, length=None, label=None): ...
        def progressbar(iterable=None, length=None, label=None, show_eta=True):
            pass
    """)
    lib, a, b = _lib(tmp_path, {"pkg/__init__.py": "", "pkg/termui.py": "def progressbar(iterable=None, length=None, label=None, show_eta=True):\n    pass\n"},
                     {"pkg/termui.py": src})
    assert positional.positional_params(lib, b, "pkg/termui.py", "progressbar") == ["iterable", "length", "label", "show_eta"]
    f = contracts.Finding("pkg/termui.py", 1, "progressbar(show_eta)", "Positional parameter was moved")
    assert positional.breaks("progressbar(items, label='x')", f, lib, a, b) is False


def test_signature_reader_finds_defs_under_version_guards():
    from aftershock import positional
    import ast as _ast
    tree = _ast.parse("import sys\nif sys.version_info >= (3, 11):\n    def f(a, b): ...\nelse:\n    def f(a): ...\n")
    assert positional.find_def(tree, "f").args.args[0].arg == "a"


def test_subclass_defining_the_member_and_getattr_classes(tmp_path):
    """huggingface_hub 3b306043 moved fields from HfFileSystemResolvedPath down into a subclass that defines them;
    werkzeug's Authorization serves attributes through __getattr__."""
    from aftershock import rules
    (tmp_path / "c.py").write_text(textwrap.dedent("""
        class HfFileSystemResolvedRepositoryPath(HfFileSystemResolvedPath):
            path_in_repo: str
            def unresolve(self):
                return self.path_in_repo
        class Other(HfFileSystemResolvedPath):
            def show(self):
                return self.path_in_repo
    """))
    f = contracts.Finding("pkg/fs.py", 1, "HfFileSystemResolvedPath.path_in_repo", "Public object was removed")
    assert sorted(s.line for s in reach.candidates(tmp_path, [f])) == [8]      # only the subclass that does NOT define it
    lib, a, b = _lib(tmp_path / "g", {"pkg/__init__.py": "", "pkg/auth.py": "class Authorization:\n    username = None\n"},
                     {"pkg/auth.py": "class Authorization:\n    def __getattr__(self, name):\n        return self.parameters.get(name)\n"})
    assert rules.inherited(contracts.Finding("pkg/auth.py", 1, "Authorization.username", "Public object was removed"), lib, b)


def test_head_scan_binds_judges_calls_against_todays_signature():
    import ast as _ast, importlib.util, sys
    spec = importlib.util.spec_from_file_location("head_scan", "spike/head_scan.py"); hs = importlib.util.module_from_spec(spec); spec.loader.exec_module(hs)
    fn = _ast.parse("class Flask:\n    def finalize_request(self, ctx, rv, from_error_handler=False): ...\n").body[0].body[0]
    n = {"finalize_request"}
    assert hs.binds("self.finalize_request(ctx, rv, from_error_handler=True)", fn, n) is True      # flask after #5818
    assert hs.binds("self.finalize_request(rv)", fn, n) is False                                    # pre-#5818 shape: missing rv
    assert hs.binds("self.finalize_request(ctx, rv, nope=1)", fn, n) is False
    assert hs.binds("self.finalize_request(ctx, rv=rv, ctx=ctx)", fn, n) is False                    # given twice
    assert hs.binds("self.finalize_request(*args)", fn, n) is None
    # a module-level function whose first parameter is named `cls` (pydantic's model_json_schema)
    fn2 = _ast.parse("def model_json_schema(cls, by_alias=True, mode='validation'): ...\n").body[0]
    assert hs.binds("return model_json_schema(cls, by_alias=b, mode=m)", fn2, {"model_json_schema"}, method=False) is True
    # werkzeug's TemplateResponse.__call__ is a definition, not a call to SharedDataMiddleware.__call__
    assert hs.is_override("def __call__(self, environ, start_response):\n    return super().__call__(environ, start_response)")
    assert not hs.is_override("return super().__call__(environ, start_response)")


def test_member_is_not_reached_by_bare_name_or_a_same_named_class_elsewhere(tmp_path):
    """werkzeug's Headers.set changed: the builtin `set(...)` is not it. pydantic/v1/mypy.py has its own copy of
    pydantic/mypy.py's class: its `self.` calls are not the changed one."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "net.py").write_text("def f(h, xs):\n    s = set(xs)\n    h.set('a', 'b')\n")
    (tmp_path / "pkg" / "mypy.py").write_text("class T:\n    def run(self):\n        return self.get_args(1, 2)\n")
    (tmp_path / "pkg" / "v1").mkdir()
    (tmp_path / "pkg" / "v1" / "mypy.py").write_text("class T:\n    def get_args(self, a, b): ...\n    def run(self):\n        return self.get_args(1, 2)\n")
    f_set = contracts.Finding("pkg/headers.py", 1, "Headers.set(_key)", "Parameter kind was changed")
    f_args = contracts.Finding("pkg/mypy.py", 1, "T.get_args(typed)", "Positional parameter was moved")
    got = {(s.file, s.line) for s in reach.candidates(tmp_path, [f_set, f_args])}
    assert ("pkg/net.py", 2) not in got and ("pkg/net.py", 3) in got
    assert ("pkg/mypy.py", 3) in got and ("pkg/v1/mypy.py", 4) not in got
    # the v1 copy also calls its own same-named class's constructor
    (tmp_path / "pkg" / "v1" / "fields.py").write_text("class Field:\n    def __init__(self, a): ...\nx = Field(1)\n")
    f_init = contracts.Finding("pkg/fields.py", 1, "Field.__init__(strict)", "Parameter was added as required")
    assert not [s for s in reach.candidates(tmp_path, [f_init]) if s.file == "pkg/v1/fields.py"]


def test_rules_only_install_without_the_model_extra(monkeypatch, capsys):
    """`pip install aftershock` without [model]: nothing imports the SDK at load time, and undecided spans go to review."""
    from aftershock import verify
    monkeypatch.setattr(verify, "TypeSafeClient", None)
    monkeypatch.setenv("TYPESAFE_API_KEY", "set-but-unusable")
    assert verify.verify([{"x": 1}]) == [verify.SKIPPED]
    assert 'pip install "aftershock[model]"' in capsys.readouterr().err


def test_self_call_pairs_with_the_subclass_own_changed_member(tmp_path):
    """click 4ece17a1 added `ctx` to Parameter.make_metavar and to Argument's override; `self.make_metavar()` inside
    Argument calls the override."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(textwrap.dedent("""
        class Parameter:
            def make_metavar(self, ctx): ...
        class Argument(Parameter):
            def make_metavar(self, ctx): ...
            def help(self):
                return self.make_metavar()
    """))
    fs = [contracts.Finding("pkg/core.py", 1, "Parameter.make_metavar(ctx)", "Parameter was added as required"),
          contracts.Finding("pkg/core.py", 1, "Argument.make_metavar(ctx)", "Parameter was added as required")]
    got = {(s.line, s.finding.symbol) for s in reach.candidates(tmp_path, fs)}
    assert (7, "Argument.make_metavar(ctx)") in got


def test_receiver_typed_as_another_library_class_is_ambiguous(tmp_path):
    """werkzeug examples: `url_map = Map(...)`; `url_map.add(...)` is Map.add, not the changed Headers.add."""
    (tmp_path / "app.py").write_text(textwrap.dedent("""
        from werkzeug.routing import Map
        from werkzeug.datastructures import Headers
        url_map = Map([])
        h = Headers()
        url_map.add(rule)
        h.add("k", "v")
    """))
    f = contracts.Finding("src/werkzeug/datastructures/headers.py", 1, "Headers.add(_key)", "Parameter was removed")
    amb = {s.line: s.ambiguous for s in reach.candidates(tmp_path, [f])}
    assert amb.get(6) is True and amb.get(7) is False


def test_head_scan_follows_a_class_whose_module_moved(tmp_path):
    """werkzeug moved Authorization from auth_headers.py to auth.py and kept `username`: not still gone."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("head_scan", "spike/head_scan.py"); hs = importlib.util.module_from_spec(spec); spec.loader.exec_module(hs)
    lib, a, b = _lib(tmp_path / "g", {"pkg/__init__.py": "", "pkg/auth_headers.py": "class Authorization:\n    username = None\n"},
                     {"pkg/auth_headers.py": None, "pkg/auth.py": "class Authorization:\n    @property\n    def username(self):\n        return 1\n"})
    f = contracts.Finding("pkg/auth_headers.py", 1, "Authorization.username", "Public object was removed")
    assert hs.still_gone(f, lib, b) is False
    g = contracts.Finding("pkg/auth_headers.py", 1, "Authorization.password", "Public object was removed")
    assert hs.still_gone(g, lib, b) is True


def test_same_named_base_class_and_subclass_reach_the_member(tmp_path):
    """werkzeug's sansio Request reads `self.encoding_errors`, defined by wrappers' Request (a subclass of the same
    name); a consumer's `class Request(lib.Request)` uses the library's member. Both count; a class that defines the
    member itself is paired with its own finding."""
    (tmp_path / "pkg" / "sansio").mkdir(parents=True)
    (tmp_path / "pkg" / "sansio" / "request.py").write_text("class Request:\n    def init(self):\n        return self.url_charset\n")
    (tmp_path / "app.py").write_text("from pkg import wrappers\nclass Request(wrappers.Request):\n    def f(self):\n        return self.url_charset\n")
    (tmp_path / "pkg" / "base.py").write_text("class Request:\n    @property\n    def encoding_errors(self): ...\n    def init(self):\n        return self.encoding_errors\n")
    fs = [contracts.Finding("pkg/wrappers.py", 1, "Request.url_charset", "Public object was removed"),
          contracts.Finding("pkg/wrappers.py", 1, "Request.encoding_errors", "Public object was removed"),
          contracts.Finding("pkg/base.py", 1, "Request.encoding_errors", "Public object was removed")]
    got = {(s.file, s.line, s.finding.path) for s in reach.candidates(tmp_path, fs)}
    assert ("pkg/sansio/request.py", 3, "pkg/wrappers.py") in got and ("app.py", 4, "pkg/wrappers.py") in got
    assert ("pkg/base.py", 5, "pkg/base.py") in got and ("pkg/base.py", 5, "pkg/wrappers.py") not in got
