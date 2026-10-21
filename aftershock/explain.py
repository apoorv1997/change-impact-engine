"""Explanation layer (plan Phase 5): prose for an engineer, over KEPT verdicts only.

The model never sees the repository and never sees unverified candidates. It receives the change,
the verified consequences (each with its file:line, the finding, how it was decided and the consumer
code that was judged) and the review list, and writes what breaks, why, and the smallest fix. A
deterministic check afterwards rejects any file:line the model mentions that was not in its input —
the explanation may only ever restate evidence. It is optional (`--explain`), budget-capped, and
scored separately from verification: a fluent explanation never compensates for a wrong verdict."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

PRICE = {  # USD per token, input / output — Claude API list prices
    "claude-opus-5": (5.0 / 1e6, 25.0 / 1e6),
    "claude-sonnet-5": (2.0 / 1e6, 10.0 / 1e6),
    "claude-haiku-4-5": (1.0 / 1e6, 5.0 / 1e6),
}
DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """You explain the consequences of a library change to the engineer who owns the consumer code.

You are given the change, a list of VERIFIED consequences (each already decided by a rule or a calibrated
check — do not re-judge them) with the consumer code that was examined, and a list of items that a human
still has to confirm. Write for an engineer who has not seen the change.

Rules:
- Only talk about the items you were given. Reference locations exactly as given, as `file:line`.
  Never mention a file or line that is not in the input. Never invent a consumer, a symptom, or a fix
  for code you were not shown.
- Group by finding. For each: what changed in one sentence; EVERY verified location, one per line as
  `file:line` — never "multiple locations" or "and others"; why it breaks, in terms of the shown code; the
  smallest change that fixes it (name the replacement API or parameter only if the change description
  makes it evident — otherwise say the replacement must be looked up).
- Items marked "for a human" appear ONLY under a final "For a human" heading, one line each: where, and
  what a person should check. Never describe them as affected, breaking or a consequence.
- No severity levels, no scores, no percentages, no praise, no filler. Plain prose and short lists.
- If the input has no verified consequences, say so in one sentence and stop."""


@dataclass
class Explanation:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float
    unknown_refs: list[str]      # file:line the model mentioned that were not in its input
    refused: bool = False
    unknown_apis: list[str] = None   # `package.Name(` the model proposed that the new library does not define


def build_input(change: dict, kept: list[dict], review: list[dict]) -> dict:
    """The only thing the model sees. `kept`/`review` items: file, line, finding, how, code (comment-free)."""
    return {
        "change": {k: v for k, v in change.items() if k != "public_names"},
        "verified_consequences": [
            {"location": f"{k['file']}:{k['line']}", "finding": k["finding"], "decided_by": k["how"],
             "consumer_code": k["code"][:1200]} for k in kept],
        "for_a_human": [{"location": f"{r['file']}:{r['line']}", "finding": r["finding"], "reason": r.get("reason", "")}
                        for r in review],
    }


_LOC = re.compile(r"`?([\w./\\-]+\.py):(\d+)`?")


_API = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")   # every `pkg.Name(` in the prose, not just the first per span


def unknown_apis(text: str, package: str, names: set[str]) -> list[str]:
    """`package.Something(` mentioned in the prose where Something is not defined by the package at the
    new ref — an invented replacement API (a local 7B model proposed `httpx.Proxies(...)`)."""
    found = {f"{m.group(1)}.{m.group(2)}" for m in _API.finditer(text) if m.group(1) == package}
    return sorted(a for a in found if a.split(".")[1] not in names)


def unknown_locations(text: str, allowed: set[str]) -> list[str]:
    """file:line references in the prose that were not in the input — the hallucination check."""
    found = {f"{m.group(1)}:{m.group(2)}" for m in _LOC.finditer(text)}
    return sorted(loc for loc in found if loc not in allowed)


# ----------------------------------------------------------------------------- template backend
_KIND = [
    ("Parameter was removed",
     "`{obj}` no longer accepts `{param}`. Every call that passes it raises `TypeError: unexpected keyword "
     "argument '{param}'` on upgrade.",
     "stop passing `{param}`; if its behaviour is still needed, use the replacement named in the library's "
     "changelog for this release."),
    ("Public object was removed",
     "`{obj}` no longer exists. Importing it fails at import time (`ImportError`); referencing it as an "
     "attribute fails at that line (`AttributeError`); a class that subclasses it fails when the module loads.",
     "replace the reference with the object that took its place, or remove the dependency on it."),
    ("Positional parameter was moved",
     "`{obj}` changed the position of `{param}`. Calls that pass arguments positionally past that point now "
     "bind them to different parameters, or raise `TypeError` if the slot became keyword-only.",
     "pass the affected arguments by keyword."),
    ("Parameter was added as required",
     "`{obj}` now requires `{param}`. Calls that do not supply it raise `TypeError: missing required "
     "argument`; an override without it breaks when the library calls it with the new argument.",
     "supply `{param}` at every call, and add it to overriding signatures."),
    ("Parameter kind was changed",
     "`{param}` of `{obj}` changed how it may be passed ({how}). Calls using the form the new signature "
     "forbids raise `TypeError`.",
     "pass `{param}` the way the new signature requires."),
]


def _mechanism(finding: str) -> tuple[str, str]:
    """(what breaks and why, smallest fix) from the finding text alone — no model, no guessing."""
    head, _, msg = finding.partition(": ")
    obj, param = head, None
    m = re.match(r"(.+)\((\w+)\)$", head)
    if m:
        obj, param = m.group(1), m.group(2)
    if obj.endswith(".__init__"):
        obj = obj[: -len(".__init__")]
    how = msg.split(":", 1)[1].strip() if "kind was changed" in msg and ":" in msg else ""
    for key, why, fix in _KIND:
        if key in msg:
            return why.format(obj=obj, param=param, how=how), fix.format(obj=obj, param=param)
    return f"`{obj}` changed: {msg}.", "review the call against the new signature."


def explain_template(change: dict, kept: list[dict], review: list[dict]) -> str:
    """Deterministic prose: grouped by finding, mechanism from the finding kind, locations verbatim."""
    lib = change.get("library", "the library")
    out = [f"{lib} {change.get('old_version', '')} → {change.get('new_version', '')}: "
           f"{len(kept)} verified consequence{'s' if len(kept) != 1 else ''}, {len(review)} for a human."]
    if not kept and not review:
        return out[0] + " Nothing in the scanned code is affected by the API changes found."
    by = {}
    for k in kept:
        by.setdefault(k["finding"], []).append(k)
    for finding, items in by.items():
        why, fix = _mechanism(finding)
        out.append(f"\n**{finding}**\n{why}")
        for it in items:
            stmt = (it.get("stmt") or it["code"]).strip().splitlines()[0] if (it.get("stmt") or it["code"]).strip() else ""
            out.append(f"- `{it['file']}:{it['line']}` — `{stmt[:100]}`  ({it['how']})")
        out.append(f"Fix: {fix}")
    if review:
        out.append("\n**For a human to confirm**")
        for r in review:
            out.append(f"- `{r['file']}:{r['line']}` — {r['finding']} — {r.get('reason', '')}")
    return "\n".join(out)


# ----------------------------------------------------------------------------- ollama backend
def _ollama(model: str, system: str, body: str, host: str = "http://localhost:11434") -> tuple[str, int, int]:
    """Local open-source model through Ollama's native chat API; returns (text, prompt_tokens, output_tokens)."""
    import urllib.request
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps({"model": model, "stream": False, "options": {"temperature": 0},
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": body}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    return d["message"]["content"], d.get("prompt_eval_count", 0), d.get("eval_count", 0)


OLLAMA_DEFAULT = "qwen2.5-coder:7b"


def ollama_pick(host: str = "http://localhost:11434") -> str:
    """The model to use when none is named: a pulled *coder* model if there is one, else the first pulled,
    else the default tag (which Ollama will refuse until it is pulled)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001 — server down: let the chat call raise the real error
        return OLLAMA_DEFAULT
    return next((n for n in names if "coder" in n), names[0] if names else OLLAMA_DEFAULT)


def _flag(text: str, allowed: set[str], change: dict, kept_locs: set[str] = frozenset(),
          review_locs: set[str] = frozenset()) -> tuple[str, list[str], list[str]]:
    bad = unknown_locations(text, allowed)
    # completeness: a verified location the prose never names is appended, so no consequence is lost to summarising
    missing = sorted(l for l in kept_locs if l not in text)
    if missing:
        text += "\n\n> aftershock: verified consequences the model did not list — they stand regardless: " + ", ".join(f"`{m}`" for m in missing)
    # attribution: a review item named more than once was very likely described as a consequence somewhere above
    over = sorted(l for l in review_locs if text.count(l) > 1)
    if over:
        text += "\n\n> aftershock: these locations are for a human to confirm, not verified consequences, wherever the prose above says otherwise: " + ", ".join(f"`{o}`" for o in over)
    apis = unknown_apis(text, change.get("library", ""), set(change.get("public_names") or [])) if change.get("public_names") else []
    if bad:
        text += "\n\n> aftershock: the model referenced locations that were not in its input and must be ignored: " + ", ".join(bad)
    if apis:
        text += ("\n\n> aftershock: the model proposed APIs that " + change.get("library", "the library") + " " +
                 str(change.get("new_version", "")) + " does not define — treat these fixes as unverified: " + ", ".join(apis))
    return text, bad, apis


def explain(change: dict, kept: list[dict], review: list[dict], model: str | None = None,
            max_usd: float = 0.05, client=None, backend: str = "template") -> Explanation:
    """backend: template (deterministic, free, default) | ollama (local open model, free) | anthropic (paid).
    The paid backend refuses to run if the input alone would exceed `max_usd` at list price."""
    payload = build_input(change, kept, review)
    if backend == "template":
        return Explanation(explain_template(change, kept, review), "template", 0, 0, 0.0, [])
    body = json.dumps(payload, indent=1)
    allowed = {x["location"] for x in payload["verified_consequences"]} | {x["location"] for x in payload["for_a_human"]}
    if backend == "ollama":
        model = model or ollama_pick()
        text, tin, tout = _ollama(model, SYSTEM, body)
        kept_locs = {x["location"] for x in payload["verified_consequences"]}
        review_locs = {x["location"] for x in payload["for_a_human"]}
        text, bad, apis = _flag(text.strip(), allowed, change, kept_locs, review_locs)
        return Explanation(text, f"ollama/{model}", tin, tout, 0.0, bad, unknown_apis=apis)
    model = model or DEFAULT_MODEL
    body = json.dumps(payload, indent=1)
    price_in, price_out = PRICE.get(model, PRICE[DEFAULT_MODEL])  # anthropic backend only
    est_in = len(body) // 3 + len(SYSTEM) // 3          # rough: ~3 chars/token on JSON
    if est_in * price_in > max_usd:
        return Explanation(f"(explanation skipped: input alone is ~{est_in} tokens ≈ ${est_in * price_in:.3f}, over the cap ${max_usd:.2f})",
                           model, 0, 0, 0.0, [])
    import anthropic                                    # only now: the cap path must not need the SDK installed
    client = client or anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": body}],
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    usage = response.usage
    usd = usage.input_tokens * price_in + usage.output_tokens * price_out
    if response.stop_reason == "refusal":
        return Explanation("(explanation refused by the model's safety layer; the verdicts above stand on their own)",
                           response.model, usage.input_tokens, usage.output_tokens, usd, [], refused=True)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    kept_locs = {x["location"] for x in payload["verified_consequences"]}
    review_locs = {x["location"] for x in payload["for_a_human"]}
    text, bad, apis = _flag(text, allowed, change, kept_locs, review_locs)
    return Explanation(text, response.model, usage.input_tokens, usage.output_tokens, usd, bad, unknown_apis=apis)


# (the `allowed` set for the anthropic path is rebuilt above from the same payload)
