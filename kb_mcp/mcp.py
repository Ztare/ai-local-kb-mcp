"""Minimal MCP (Streamable HTTP, stateless, JSON responses) on Tornado handlers (KB plugin#transport)."""
import json
import os

from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import APIHandler
from tornado import web

from . import render
from .kb import PROFILES, Ctx, KbError
from .store import parse_source, src_of

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
# MCP features a client may handle badly; switched off per connection by `off=` (KB api#connection)
OFF_FEATURES = ("structured", "instructions")


def parse_off(value):
    """'structured, instructions' -> frozenset of known feature names; unknown ones are ignored."""
    return frozenset(x for x in (p.strip().lower() for p in (value or "").split(",")) if x in OFF_FEATURES)
SERVER_INFO = {"name": "kb-mcp", "version": "0.1.0"}

S = {"type": "string"}
B = {"type": "boolean"}
A = {"type": "array", "items": {"type": "string"}}
SECS = {"type": "array", "items": {"type": "object", "properties": {
    "id": S, "title": S, "content": S,
    "tags": {"type": "array", "items": {"type": "object", "properties": {"text": S, "source": S}}}},
    "required": ["id"]}}

# name -> (properties, required) for std/full; lite overrides below
SCHEMAS = {
    "index": ({"path": S}, []),
    "get": ({"path": S, "with_tags": B, "full": B}, ["path"]),
    "search": ({"substrings": A, "path": S}, ["substrings"]),
    "search_regex": ({"pattern": S, "path": S}, ["pattern"]),
    "section_tags": ({"path": S}, ["path"]),
    "search_section_tags": ({"substrings": A, "path": S}, ["substrings"]),
    "search_section_tags_regex": ({"pattern": S, "path": S}, ["pattern"]),
    "sections_by_tag": ({"tag": S, "path": S, "with_tags": B}, ["tag"]),
    "save_topic": ({"path": S, "title": S, "content": S}, ["path", "title", "content"]),
    "save_article": ({"path": S, "title": S, "content": S}, ["path", "title", "content"]),
    "save_section": ({"path": S, "title": S, "content": S}, ["path", "title", "content"]),
    "replace_lines": ({"path": S, "old": S, "new": S}, ["path", "old", "new"]),
    "add_section_tags": ({"path": S, "tags": A}, ["path", "tags"]),
    "drop_section_tags": ({"path": S, "tags": A}, ["path", "tags"]),
    "delete": ({"path": S}, ["path"]),
    "unindexed": ({"path": S}, []),
    "get_full": ({"path": S}, ["path"]),
    "put_article": ({"path": S, "sections": SECS, "version": S}, ["path", "sections", "version"]),
    "put_sections": ({"path": S, "sections": SECS, "version": S}, ["path", "sections", "version"]),
}
LITE_SCHEMAS = {
    "get": ({"path": S}, ["path"]),
    "search": ({"text": S}, ["text"]),
}
ORDER = ["index", "get", "search", "search_regex", "section_tags", "search_section_tags",
         "search_section_tags_regex", "sections_by_tag", "save_topic", "save_article",
         "save_section", "replace_lines", "add_section_tags", "drop_section_tags", "delete",
         "unindexed", "get_full", "put_article", "put_sections"]
ALIASES = {"text": ["substrings", "query", "q"], "substrings": ["text", "query", "q"],
           "path": ["id"], "tags": ["tag"]}

ADMIN_DOCS = {
    "_head": "Knowledge base - maintenance API for the indexer. Same as the consumer API (full) "
             "plus all section metadata and whole-article writes. Read with get_full, write back "
             "with put_article / put_sections passing the version you read; on `conflict` read again.",
    "unindexed": "Articles and topics that have sections with indexed=false or no head, oldest first. "
                 "Parameters: path (optional).",
    "get_full": "Whole article (or topic head) with all metadata of every section, in file order, "
                "plus `version`. Parameters: path (required).",
    "put_article": "Overwrite the whole article: head (id 'head', with title) + sections in the wanted order; "
                   "each {id, title, content, tags}. Tags: strings (source=indexer) or {text, source}. "
                   "Omit title / content of an existing id to keep its current text (e.g. tags-only change). "
                   "All sections get indexed=true. Parameters: path, sections, version (from get_full).",
    "put_sections": "Write some sections by id (new id -> appended at the end); others are untouched. "
                    "Omit title / content of an existing id to keep its current text. "
                    "Written sections get indexed=true. Parameters: path, sections, version (from get_full).",
}


class Docs:
    """Tool descriptions from mcp_docs/<lang>/<profile>.ipynb via the ContentsManager (KB plugin#mcp-docs)."""

    def __init__(self, cm, root="mcp_docs"):
        self.cm = cm
        self.root = root.strip("/")
        self.cache = {}

    async def load(self, lang, profile):
        f = "%s/%s/%s.ipynb" % (self.root, lang, profile)
        try:
            lm = (await ensure_async(self.cm.get(f, content=False)))["last_modified"]
        except Exception:
            return {}
        hit = self.cache.get(f)
        if hit and hit[0] == lm:
            return hit[1]
        out = {}
        try:
            nb = (await ensure_async(self.cm.get(f, content=True, type="notebook")))["content"]
            for c in nb.cells:
                kb = (c.get("metadata") or {}).get("kb") or {}
                sid = kb.get("id")
                if sid:
                    title, _, content = parse_source(src_of(c))
                    out[sid] = {"title": title, "content": content, "source": src_of(c)}
        except Exception:
            out = {}
        self.cache[f] = (lm, out)
        return out


async def _texts(docs, lang, profile):
    return await docs.load(lang, profile) or await docs.load("en", profile)


async def tool_list(ctx, docs):
    admin = ctx.profile == "admin"
    d = await _texts(docs, ctx.lang, "full" if admin else ctx.profile)
    a = await _texts(docs, ctx.lang, "admin") if admin else {}
    out = []
    for name in ORDER:
        if not ctx.has(name):
            continue
        props, req = (LITE_SCHEMAS.get(name) if ctx.profile == "lite" else None) or SCHEMAS[name]
        key = name.replace("_", "-")
        if name in ADMIN_DOCS:
            sec = a.get(key)
            desc = sec["content"] if sec else ADMIN_DOCS[name]
        else:
            sec = d.get(key)
            desc = sec["content"] if sec else name
        out.append({"name": name, "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": req}})
    return out


async def instructions(ctx, docs):
    """Head of mcp_docs/<lang>/<profile> (admin -> admin.ipynb, the indexer rules; KB admin-api#rules)."""
    h = (await _texts(docs, ctx.lang, ctx.profile)).get("head")
    if h:
        return h["content"]
    return ADMIN_DOCS["_head"] if ctx.profile == "admin" else ""


def _arg(args, name):
    if name in args:
        return args[name]
    for a in ALIASES.get(name, []):
        if a in args:
            return args[a]
    return None


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def usage(ctx, name):
    props, req = (LITE_SCHEMAS.get(name) if ctx.profile == "lite" else None) or SCHEMAS[name]
    return "%s(%s)" % (name, ", ".join(p if p in req else p + "?" for p in props))


async def call_tool(kb, ctx, name, args):
    """-> (result dict) or raises KbError."""
    if name not in SCHEMAS:
        raise KbError("not_allowed", ctx.t("unknown_tool", name=name), ctx.t("hint_not_in_profile"))
    if not ctx.has(name):
        where = [p for p in ("lite", "std", "full") if name in PROFILES[p]]
        msg = ctx.t("not_in_profile_where", name=name, profile=ctx.profile, where=", ".join(where)) \
            if where else ctx.t("not_in_profile", name=name, profile=ctx.profile)
        raise KbError("not_allowed", msg, ctx.t("hint_not_in_profile"))
    args = args if isinstance(args, dict) else {}
    props, req = (LITE_SCHEMAS.get(name) if ctx.profile == "lite" else None) or SCHEMAS[name]
    v = {}
    for p in props:
        val = _arg(args, p)
        if val is None and p in req:
            raise KbError("invalid_input", ctx.t("missing_param", name=p),
                          ctx.t("hint_usage", usage=usage(ctx, name)))
        v[p] = val
    async with kb.lock:
        await kb.refresh()
        return await _dispatch(kb, ctx, name, v)


async def _dispatch(kb, ctx, name, v):
    if name == "get":
        return kb.get(ctx, v["path"] or "", _bool(v.get("with_tags")), _bool(v.get("full")))
    if name == "index":
        return kb.index(ctx, v["path"])
    if name == "search":
        return kb.search(ctx, v.get("text") if ctx.profile == "lite" else v["substrings"], v.get("path"))
    if name in ("search_regex", "search_section_tags_regex"):
        return getattr(kb, name)(ctx, v["pattern"], v["path"])
    if name == "section_tags":
        return kb.section_tags(ctx, v["path"])
    if name == "search_section_tags":
        return kb.search_section_tags(ctx, v["substrings"], v["path"])
    if name == "sections_by_tag":
        return kb.sections_by_tag(ctx, v["tag"], v["path"], _bool(v.get("with_tags")))
    if name in ("save_topic", "save_article", "save_section"):
        return await getattr(kb, name)(ctx, v["path"], v["title"], v["content"])
    if name == "replace_lines":
        return await kb.replace_lines(ctx, v["path"], v["old"], v["new"])
    if name in ("add_section_tags", "drop_section_tags"):
        return await getattr(kb, name)(ctx, v["path"], v["tags"])
    if name == "delete":
        return await kb.delete(ctx, v["path"])
    if name == "unindexed":
        return kb.unindexed(ctx, v["path"])
    if name == "get_full":
        return kb.get_full(ctx, v["path"])
    return await getattr(kb, name)(ctx, v["path"], v["sections"], v["version"])


def _result(ctx, text, structured, is_error):
    out = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if "structured" not in ctx.off:
        out["structuredContent"] = structured
    return out


async def tool_result(kb, ctx, name, args, log=None):
    try:
        res = await call_tool(kb, ctx, name, args)
        return _result(ctx, render.text(res, ctx), render.structured(res, ctx), False)
    except KbError as e:
        return _result(ctx, render.error_text(e), render.error_structured(e), True)
    except Exception as e:  # never leak internals as a protocol error
        if log:
            log.exception("kb-mcp: tool %s failed", name)
        err = KbError("internal", str(e) or e.__class__.__name__)
        return _result(ctx, render.error_text(err), render.error_structured(err), True)


async def handle_rpc(kb, docs, ctx, msg, log=None):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    mid = msg.get("id")
    method = msg["method"]
    params = msg.get("params") or {}
    if "id" not in msg:
        return None  # notification
    if method == "initialize":
        want = params.get("protocolVersion")
        result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": SERVER_INFO}
        if "instructions" not in ctx.off:
            result["instructions"] = await instructions(ctx, docs)
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": await tool_list(ctx, docs)}
    elif method == "tools/call":
        result = await tool_result(kb, ctx, params.get("name"), params.get("arguments") or {}, log)
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: " + str(method)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class McpHandler(APIHandler):
    def initialize(self, kb, docs, admin=False):
        self.kb = kb
        self.docs = docs
        self.admin = admin

    def _ctx(self):
        profile = "admin" if self.admin else self.get_query_argument("profile", "full")
        off = parse_off(self.get_query_argument("off", os.environ.get("KB_MCP_OFF", "")))
        return Ctx(profile, self.get_query_argument("lang", "en"), self.get_query_argument("limit", None), off)

    def check_xsrf_cookie(self):  # token-authenticated machine clients
        return

    @web.authenticated
    async def post(self):
        try:
            msg = json.loads(self.request.body or b"null")
        except ValueError:
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        try:
            ctx = self._ctx()
        except ValueError:
            ctx = Ctx()
        if isinstance(msg, list):
            out = []
            for m in msg:
                c = Ctx(ctx.profile, ctx.lang, ctx.limit, ctx.off)
                r = await handle_rpc(self.kb, self.docs, c, m, self.log)
                if r is not None:
                    out.append(r)
            if not out:
                self.set_status(202)
                return self.finish()
            return self._json(out)
        r = await handle_rpc(self.kb, self.docs, ctx, msg, self.log)
        if r is None:
            self.set_status(202)
            return self.finish()
        return self._json(r)

    @web.authenticated
    def get(self):
        self.set_status(405)
        self.set_header("Allow", "POST")
        self.finish()

    @web.authenticated
    def delete(self):
        self.set_status(405)
        self.finish()

    def _json(self, obj):
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.finish(json.dumps(obj, ensure_ascii=False))


class DocsHandler(web.RequestHandler):
    """GET /mcp/docs/<lang>/<profile>[/<id>] - plain markdown, no auth (KB plugin#mcp-docs)."""

    def initialize(self, docs):
        self.docs = docs

    async def get(self, lang, profile, sid=None):
        d = await self.docs.load(lang, profile)
        sec = d.get(sid or "head")
        if not sec:
            raise web.HTTPError(404)
        self.set_header("Content-Type", "text/markdown; charset=utf-8")
        self.finish(sec["source"])
