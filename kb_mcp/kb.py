"""Knowledge base: in-memory index over notebooks + consumer and admin operations."""
import asyncio
import contextvars
import difflib
import json
import re

import nbformat
from jupyter_core.utils import ensure_async

from . import store
from .store import NAME_RE, Doc
from .texts import t, translit

MAX_DEPTH = 3
LITE = {"get", "search", "save_article", "save_section"}
STD = LITE | {"index", "section_tags", "search_section_tags", "sections_by_tag",
              "save_topic", "add_section_tags", "replace_lines"}
FULL = STD | {"search_regex", "search_section_tags_regex", "drop_section_tags", "delete"}
ADMIN = FULL | {"unindexed", "get_full", "put_article", "put_sections"}
PROFILES = {"lite": LITE, "std": STD, "full": FULL, "admin": ADMIN}
LIMITS = {"lite": 5, "std": 10, "full": 20, "admin": 20}


class KbError(Exception):
    def __init__(self, code, message, hint="", options=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.options = options or []


class Ctx:
    def __init__(self, profile="full", lang="en", limit=None, off=()):
        self.profile = profile if profile in PROFILES else "full"
        self.lang = lang if lang in ("en", "ru") else "en"
        self.limit = int(limit) if limit else LIMITS[self.profile]
        self.off = frozenset(off)  # disabled MCP features (KB api#connection)
        self.normalized = None

    def has(self, cmd):
        return cmd in PROFILES[self.profile]

    def t(self, key, **kw):
        return t(self.lang, key, **kw)

    def call(self, name, *args):
        parts = []
        for a in args:
            if isinstance(a, list):
                parts.append("[" + ", ".join('"%s"' % x for x in a) + "]")
            elif a is ...:
                parts.append("...")
            else:
                parts.append('"%s"' % a)
        return "%s(%s)" % (name, ", ".join(parts))

    def search_call(self, text):
        if self.profile == "lite":
            return self.call("search", text)
        return self.call("search", [text])


def nk(s):
    """Lookup key: case/space/underscore-insensitive."""
    return re.sub(r"[\s_]+", "-", s.strip()).casefold()


def parent_of(p):
    return p.rsplit("/", 1)[0] if "/" in p else ""


def norm_name(s):
    return re.sub(r"[\s_]+", "-", s.strip()).lower()


def suggest_name(s):
    return re.sub(r"[^a-z0-9]+", "-", translit(s)).strip("-") or "name"


class Ref:
    """Parsed path from a client."""

    def __init__(self, raw):
        s = str(raw if raw is not None else "").strip().strip("\"'`").strip()
        self.raw = s
        s = s.replace("\\", "/")
        p, hash_, sid = s.partition("#")
        parts = [x.strip() for x in p.split("/") if x.strip()]
        if parts and parts[-1].lower().endswith(".ipynb"):
            parts[-1] = parts[-1][:-6].strip()
        if parts and parts[-1].lower() == "topic" and len(parts) > 1:
            parts.pop()
        self.parts = [x for x in parts if x]
        sid = sid.strip()
        self.sid = sid if sid else None
        self.has_hash = bool(hash_)

    @property
    def obj(self):
        return "/".join(self.parts)


WRITER = contextvars.ContextVar("kb_writer", default="web")


VERSION_IGNORED_KB = ("updated", "changed_by", "indexed", "hash")
PH = "*"


def content_version(nb):
    """Version of an article = hash of its meaningful content (KB admin-api#version).

    Hashed is a copy where irrelevant values are replaced with placeholders: notebook metadata
    (kernelspec, language_info...), nbformat, Jupyter's own cell id, non-kb cell metadata and the
    derived / bookkeeping kb fields. So a no-op save from JupyterLab does not change the version.
    """
    cells = []
    for c in nb.get("cells") or []:
        kb = dict((c.get("metadata") or {}).get("kb") or {})
        for k in VERSION_IGNORED_KB:
            kb[k] = PH          # present or not - same placeholder
        cells.append({"cell_type": c.get("cell_type"), "source": store.src_of(c),
                      "attachments": c.get("attachments") or {}, "id": PH, "metadata": {"kb": kb}})
    norm = {"cells": cells, "metadata": PH, "nbformat": PH, "nbformat_minor": PH}
    return store.text_hash(json.dumps(norm, sort_keys=True, ensure_ascii=False))


class KB:
    """Index in memory; all file access goes through Jupyter's ContentsManager (KB plugin#server-api-only)."""

    def __init__(self, cm, root="kb", who_external="external", on_change=None, log=None):
        self.cm = cm
        self.root = root.strip("/")
        self.on_change = on_change
        self.log = log
        self.lock = asyncio.Lock()
        self.topics = {}    # path -> Doc
        self.articles = {}  # path -> Doc
        self.keys = {}      # nk(path) -> ('topic'|'article', path)
        self.stats = {}     # notebook API path -> last_modified
        self.stash = {}     # API path -> notebook normalized by pre-save, waiting for post-save
        self.who_external = who_external
        self._loaded = False

    # ---------------------------------------------------------- storage/index
    def _notify(self, who, path):
        if self.on_change and self._loaded:
            try:
                self.on_change(who, path)
            except Exception:  # pragma: no cover
                if self.log:
                    self.log.exception("kb on_change failed")

    def under_root(self, p):
        p = (p or "").strip("/")
        return p == self.root or p.startswith(self.root + "/")

    def _classify(self, api_path):
        p = (api_path or "").strip("/").replace("\\", "/")
        if not p.startswith(self.root + "/"):
            return None
        parts = p[len(self.root) + 1:].split("/")
        if any(x.startswith(".") for x in parts) or not parts[-1].endswith(".ipynb"):
            return None
        dirs, fname = parts[:-1], parts[-1][:-6]
        if fname == "topic":
            if 1 <= len(dirs) <= MAX_DEPTH:
                return "topic", "/".join(dirs)
            return None
        if 2 <= len(dirs) <= MAX_DEPTH:
            return "article", "/".join(dirs + [fname])
        return None

    def file_of(self, kind, path):
        return self.root + "/" + path + ("/topic.ipynb" if kind == "topic" else ".ipynb")

    def doc_by_file(self, f):
        f = (f or "").strip("/")
        cls = self._classify(f)
        if not cls:
            return None
        d = (self.topics if cls[0] == "topic" else self.articles).get(cls[1])
        return d if d is not None and d.file == f else None

    def index_nb(self, f, nb, last_modified, who):
        """Put a just saved / read notebook into the index. Sync: called from the post-save hook."""
        f = f.strip("/")
        cls = self._classify(f)
        if cls is None:
            return None
        if not isinstance(nb, nbformat.NotebookNode):
            nb = nbformat.from_dict(nb)
        kind, path = cls
        self.stats[f] = last_modified
        doc = Doc(kind, path, f, nb, content_version(nb))
        (self.topics if kind == "topic" else self.articles)[path] = doc
        self._rekey()
        self._notify(who, path)
        return doc

    async def _get(self, path, **kw):
        return await ensure_async(self.cm.get(path, **kw))

    async def _write(self, f, nb, who):
        """Save through the ContentsManager. Its pre/post-save hooks normalize and index."""
        tok = WRITER.set(who)
        try:
            model = await ensure_async(self.cm.save({"type": "notebook", "format": "json", "content": nb}, f))
        except Exception:
            self.stats.pop(f, None)  # in-memory copy may differ from the file: reread on next refresh
            raise
        finally:
            WRITER.reset(tok)
        if self.stats.get(f) != model.get("last_modified"):  # backend without post-save hooks
            self.index_nb(f, nb, model.get("last_modified"), who)

    async def _save(self, doc, who):
        await self._write(doc.file, doc.nb, who)

    async def refresh(self):
        """Catch edits made past Jupyter (git, editor, adb) by last_modified from the ContentsManager."""
        if not await ensure_async(self.cm.dir_exists(self.root)):
            await ensure_async(self.cm.save({"type": "directory"}, self.root))
        seen = set()
        await self._walk(self.root, 0, seen, self.who_external)
        for f in [f for f in self.stats if f not in seen]:
            self._drop(f, self.who_external)
        self._rekey()
        self._loaded = True

    async def _walk(self, d, depth, seen, who):
        try:
            model = await self._get(d, content=True, type="directory")
        except Exception:
            return
        entries = sorted(model["content"], key=lambda e: e["name"])
        if 1 <= depth <= MAX_DEPTH and not any(e["name"] == "topic.ipynb" for e in entries):
            await self._make_stub(d, who)
            model = await self._get(d, content=True, type="directory")
            entries = sorted(model["content"], key=lambda e: e["name"])
        for e in entries:
            if e["name"].startswith("."):
                continue
            if e["type"] == "directory":
                if depth < MAX_DEPTH:
                    await self._walk(e["path"], depth + 1, seen, who)
            elif e["name"].endswith(".ipynb"):
                f = e["path"].strip("/")
                if self._classify(f) is None:
                    continue
                seen.add(f)
                if self.stats.get(f) != e["last_modified"]:
                    await self._load(f, who)

    async def _make_stub(self, d, who):
        name = d.rsplit("/", 1)[-1]
        nb = store.new_nb(1, name, t("en", "stub_content"))
        store.touch(nb.cells[0], who, False)
        await self._write(d + "/topic.ipynb", nb, who)

    async def _load(self, f, who):
        try:
            model = await self._get(f, content=True, type="notebook")
        except Exception as e:
            if self.log:
                self.log.warning("kb: can't read %s: %s", f, e)
            self._drop(f, who)
            return
        nb = model["content"]
        if store.ensure_meta(nb, who):
            await self._write(f, nb, who)
        else:
            self.index_nb(f, nb, model["last_modified"], who)

    def _drop(self, f, who):
        self.stats.pop(f, None)
        cls = self._classify(f)
        if cls:
            kind, path = cls
            d = self.topics if kind == "topic" else self.articles
            if path in d and d[path].file == f:
                del d[path]
                self._notify(who, path)

    def _rekey(self):
        keys = {}
        for p in self.topics:
            keys[nk(p)] = ("topic", p)
        for p in self.articles:
            keys.setdefault(nk(p), ("article", p))
        self.keys = keys

    async def on_event(self, action, path, source_path=None, who="web"):
        """Jupyter contents events: rename / delete / copy / upload / save (KB plugin#hooks)."""
        path = (path or "").strip("/")
        if action == "get":
            return
        if action in ("delete", "rename"):
            gone = ((source_path if action == "rename" else path) or "").strip("/")
            for f in [f for f in self.stats if f == gone or f.startswith(gone + "/")]:
                self._drop(f, who)
        if action != "delete" and self.under_root(path):
            try:
                model = await self._get(path, content=False)
            except Exception:
                model = None
            if model and model["type"] == "directory":
                rel = path[len(self.root):].strip("/")
                depth = len(rel.split("/")) if rel else 0
                await self._walk(path, depth, set(), who)
            elif model and self._classify(path) and self.stats.get(path) != model["last_modified"]:
                await self._load(path, who)
        self._rekey()

    # --------------------------------------------------------------- lookup
    def lookup(self, parts):
        if not parts:
            return ("root", "")
        return self.keys.get(nk("/".join(parts)))

    def doc_of(self, kind, path):
        return (self.topics if kind == "topic" else self.articles)[path]

    def child_topics(self, tp):
        return sorted(p for p in self.topics if parent_of(p) == tp)

    def child_articles(self, tp):
        return sorted(p for p in self.articles if parent_of(p) == tp)

    def _items(self, paths, kind):
        d = self.topics if kind == "topic" else self.articles
        return [{"path": p, "title": d[p].title} for p in paths]

    def _rank(self, want, items, limit):
        def key(it):
            p = it["path"] if isinstance(it, dict) else it
            return (-difflib.SequenceMatcher(None, want.casefold(), p.casefold()).ratio(), p)
        return sorted(items, key=key)[:limit]

    def _nearest_topic(self, parts):
        for n in range(len(parts) - 1, 0, -1):
            hit = self.lookup(parts[:n])
            if hit and hit[0] == "topic":
                return hit[1]
        return ""

    def _not_found_obj(self, ctx, ref):
        want = ref.obj
        anc = self._nearest_topic(ref.parts)
        if anc:
            items = self._items(self.child_topics(anc), "topic") + \
                self._items(self.child_articles(anc), "article")
            hint = ctx.t("hint_pick_or_search", call=ctx.call("get", anc),
                         search=ctx.search_call(ref.parts[-1]))
            key = "nf_article" if len(ref.parts) > 2 else "nf_topic"
        else:
            items = self._items(self.child_topics(""), "topic")
            hint = ctx.t("hint_pick_or", call=ctx.call("get", ""))
            key = "nf_topic" if len(ref.parts) <= 1 else "nf_path"
        raise KbError("not_found", ctx.t(key, path=want), hint,
                      self._rank(want, items, ctx.limit))

    def _section_not_found(self, ctx, doc, want, admin=False):
        secs = doc.sections() if admin else doc.visible()
        items = [{"path": s.path, "title": s.title or ""} for s in secs]
        raise KbError("not_found", ctx.t("nf_section", path=want),
                      ctx.t("hint_pick_or", call=ctx.call("get", doc.path)),
                      self._rank(want, items, ctx.limit))

    def _note(self, ctx, ref, canonical):
        if ref.raw != canonical:
            ctx.normalized = canonical

    def resolve(self, ctx, raw, admin=False):
        """-> (kind, doc|None, sec|None) for existing objects, else KbError."""
        ref = Ref(raw)
        hit = self.lookup(ref.parts)
        if hit is None:
            self._not_found_obj(ctx, ref)
        kind, path = hit
        if kind == "root":
            if ref.sid:
                raise KbError("not_found", ctx.t("nf_section", path=ref.raw),
                              ctx.t("hint_call", call=ctx.call("get", "")))
            self._note(ctx, ref, "")
            return "root", None, None
        doc = self.doc_of(kind, path)
        if ref.sid:
            sec = doc.find(ref.sid) or doc.find(norm_name(ref.sid))
            if sec is None:
                self._section_not_found(ctx, doc, path + "#" + ref.sid, admin)
            if not sec.visible and not admin:
                self._section_not_found(ctx, doc, path + "#" + ref.sid)
            self._note(ctx, ref, sec.path)
            return "section", doc, sec
        self._note(ctx, ref, doc.path)
        return kind, doc, None

    def scope(self, ctx, raw):
        """Docs inside a path (topic -> subtree incl. topic heads; article -> itself)."""
        if raw is None or str(raw).strip() == "":
            return list(self.topics.values()) + list(self.articles.values())
        kind, doc, sec = self.resolve(ctx, raw)
        if kind == "root":
            return list(self.topics.values()) + list(self.articles.values())
        if kind in ("article", "section") and doc.kind == "article":
            return [doc]
        tp = doc.path
        out = [d for p, d in self.topics.items() if p == tp or p.startswith(tp + "/")]
        out += [d for p, d in self.articles.items() if p.startswith(tp + "/")]
        return out

    @staticmethod
    def _order(docs):
        return sorted(docs, key=lambda d: (d.path, 0 if d.kind == "topic" else 1))

    # ------------------------------------------------------------- results
    @staticmethod
    def sec_dict(s, with_tags=False):
        d = {"path": s.path, "title": s.title, "content": s.content}
        if with_tags:
            d["tags"] = s.tags
        return d

    @staticmethod
    def sec_full(s):
        m = s.meta
        return {"path": s.path, "id": s.id, "title": s.title or "",
                "content": s.content if s.visible else store.src_of(s.cell),
                "indexed": bool(m.get("indexed")), "updated": m.get("updated", ""),
                "changed_by": m.get("changed_by", ""), "hash": m.get("hash", ""),
                "tags": [dict(tg) for tg in m.get("tags") or []]}

    # ------------------------------------------------------------ consumer
    def get(self, ctx, path="", with_tags=False, full=False):
        kind, doc, sec = self.resolve(ctx, path)
        if kind == "root":
            return {"kind": "root", "topics": self._items(self.child_topics(""), "topic")}
        if kind == "section":
            return {"kind": "section", "section": self.sec_dict(sec, with_tags)}
        head = doc.head()
        headd = self.sec_dict(head, with_tags) if head and head.visible else None
        if kind == "topic":
            return {"kind": "topic", "path": doc.path, "title": doc.title, "head": headd,
                    "topics": self._items(self.child_topics(doc.path), "topic"),
                    "articles": self._items(self.child_articles(doc.path), "article")}
        rest = [s for s in doc.visible() if s.id != "head"]
        if full:
            secs = [self.sec_dict(s, with_tags) for s in rest]
        else:
            secs = [{"path": s.path, "title": s.title} for s in rest]
        return {"kind": "article", "path": doc.path, "title": doc.title, "head": headd,
                "sections": secs, "full": bool(full)}

    def index(self, ctx, path=None):
        docs = self.scope(ctx, path)
        items = [{"path": d.path, "title": d.title}
                 for d in self._order(docs) if d.kind == "article"]
        return {"kind": "list", "items": items}

    def _iter_sections(self, docs):
        for d in self._order(docs):
            for s in d.visible():
                yield s

    def search(self, ctx, substrings, path=None):
        subs = [s.casefold() for s in as_subs(substrings)]
        docs = self.scope(ctx, path)
        items = []
        for s in self._iter_sections(docs):
            hay = (s.title + "\n" + s.content).casefold()
            if any(x in hay for x in subs):
                items.append({"path": s.path, "title": s.title})
        return {"kind": "list", "items": items, "empty_hint": ctx.t("empty_search")}

    def _regex(self, ctx, pattern):
        try:
            return re.compile(str(pattern), re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise KbError("invalid_pattern", ctx.t("invalid_pattern", err=str(e)),
                          ctx.t("hint_invalid_pattern", call=ctx.search_call("...")))

    def search_regex(self, ctx, pattern, path=None):
        rx = self._regex(ctx, pattern)
        docs = self.scope(ctx, path)
        items = [{"path": s.path, "title": s.title} for s in self._iter_sections(docs)
                 if rx.search(s.title) or rx.search(s.content)]
        return {"kind": "list", "items": items, "empty_hint": ctx.t("empty_search")}

    def _all_tags(self, docs):
        out, seen = [], set()
        for s in self._iter_sections(docs):
            for tg in s.tags:
                if tg.casefold() not in seen:
                    seen.add(tg.casefold())
                    out.append(tg)
        return sorted(out, key=str.casefold)

    def section_tags(self, ctx, path):
        if path is None or str(path).strip() == "":
            raise KbError("not_allowed", ctx.t("tags_no_path"),
                          ctx.t("hint_tags_no_path", call=ctx.call("section_tags", "<domain>")),
                          self._items(self.child_topics(""), "topic")[:ctx.limit])
        return {"kind": "tags", "tags": self._all_tags(self.scope(ctx, path))}

    def search_section_tags(self, ctx, substrings, path=None):
        subs = [s.casefold() for s in as_subs(substrings)]
        tags = [tg for tg in self._all_tags(self.scope(ctx, path))
                if any(x in tg.casefold() for x in subs)]
        return {"kind": "tags", "tags": tags, "empty_hint": ctx.t("empty_search")}

    def search_section_tags_regex(self, ctx, pattern, path=None):
        rx = self._regex(ctx, pattern)
        tags = [tg for tg in self._all_tags(self.scope(ctx, path)) if rx.search(tg)]
        return {"kind": "tags", "tags": tags, "empty_hint": ctx.t("empty_search")}

    def sections_by_tag(self, ctx, tag, path=None, with_tags=False):
        want = str(tag).strip().casefold()
        docs = self.scope(ctx, path)
        secs = [s for s in self._iter_sections(docs)
                if any(tg.casefold() == want for tg in s.tags)]
        if not secs:
            part = str(tag).strip()
            opts = [tg for tg in self._all_tags(docs)
                    if want and (want in tg.casefold() or tg.casefold() in want)]
            if ctx.has("search_section_tags"):
                hint = ctx.t("hint_tag", call=ctx.call("search_section_tags", [part[:max(3, len(part) // 2)]]))
            else:
                hint = ctx.t("hint_search", call=ctx.search_call(part))
            raise KbError("not_found", ctx.t("nf_tag", tag=part), hint,
                          self._rank(part, opts, ctx.limit))
        return {"kind": "sections", "sections": [self.sec_dict(s, with_tags) for s in secs]}

    # ---------------------------------------------------------------- write
    def _check_new_name(self, ctx, raw_name):
        name = norm_name(raw_name)
        if name == "topic":
            raise KbError("invalid_name", ctx.t("reserved_name", name=name),
                          ctx.t("hint_retry_name"), [name + "-1"])
        if not NAME_RE.match(name):
            raise KbError("invalid_name", ctx.t("invalid_name", name=raw_name),
                          ctx.t("hint_retry_name"), [suggest_name(raw_name)])
        return name

    def _need_text(self, ctx, name, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise KbError("invalid_input", ctx.t("missing_param", name=name))
        return value

    def _level3_on(self, parts):
        for n in range(min(len(parts), MAX_DEPTH), 0, -1):
            hit = self.lookup(parts[:n])
            if hit and hit[0] == "topic":
                if n == MAX_DEPTH:
                    return [{"path": hit[1], "title": self.topics[hit[1]].title}]
                return self._items([p for p in self.child_topics(hit[1])], "topic")
        return []

    def _missing_parent(self, ctx, parent_parts, create_cmd):
        ref = Ref("/".join(parent_parts))
        anc = ""
        for n in range(len(parent_parts) - 1, 0, -1):
            hit = self.lookup(parent_parts[:n])
            if hit and hit[0] == "topic":
                anc = hit[1]
                break
        items = self._items(self.child_topics(anc), "topic")
        if create_cmd and ctx.has(create_cmd):
            hint = ctx.t("hint_parent", call=ctx.call("get", anc),
                         create=ctx.call(create_cmd, ref.obj, ..., ...))
        else:
            hint = ctx.t("hint_parent_lite", call=ctx.call("get", anc))
        raise KbError("not_found", ctx.t("parent_missing", path=ref.obj), hint,
                      self._rank(ref.obj, items, ctx.limit))

    async def save_topic(self, ctx, path, title, content, who="mcp"):
        title = store.clean_title(self._need_text(ctx, "title", title))
        content = store.clean_content(title, content)
        ref = Ref(self._need_text(ctx, "path", path))
        if ref.has_hash:
            raise KbError("invalid_input", ctx.t("need_no_section", path=ref.raw))
        hit = self.lookup(ref.parts)
        if hit and hit[0] == "topic":
            doc = self.topics[hit[1]]
            return await self._write_head(ctx, ref, doc, title, content, who)
        if hit and hit[0] == "article":
            raise KbError("not_allowed", ctx.t("exists_other_kind", path=hit[1], kind=ctx.t("kind_article")),
                          ctx.t("hint_call", call=ctx.call("get", hit[1])))
        if len(ref.parts) > MAX_DEPTH:
            raise KbError("too_deep", ctx.t("too_deep"), ctx.t("hint_too_deep_topic"),
                          self._level3_on(ref.parts))
        parent = ""
        if len(ref.parts) > 1:
            ph = self.lookup(ref.parts[:-1])
            if not ph or ph[0] != "topic":
                self._missing_parent(ctx, ref.parts[:-1], "save_topic")
            parent = ph[1]
        name = self._check_new_name(ctx, ref.parts[-1])
        newp = (parent + "/" + name) if parent else name
        d = self.root + "/" + newp
        if not await ensure_async(self.cm.dir_exists(d)):
            await ensure_async(self.cm.save({"type": "directory"}, d))
        nb = store.new_nb(1, title, content)
        store.touch(nb.cells[0], who, who == "indexer")
        await self._write(d + "/topic.ipynb", nb, who)
        self._note(ctx, ref, newp)
        return {"kind": "write", "path": newp, "action": "created"}

    async def _write_head(self, ctx, ref, doc, title, content, who):
        src = store.build_source(1, title, content)
        head = doc.head()
        if head is None:
            cell = store.new_cell(src, "head")
            doc.nb.cells.insert(0, cell)
        else:
            cell = head.cell
            if store.src_of(cell) == src:
                self._note(ctx, ref, doc.path)
                return {"kind": "write", "path": doc.path, "action": "updated", "unchanged": True}
            cell["source"] = src
        store.touch(cell, who, who == "indexer")
        await self._save(doc, who)
        self._note(ctx, ref, doc.path)
        return {"kind": "write", "path": doc.path, "action": "updated"}

    async def save_article(self, ctx, path, title, content, who="mcp"):
        title = store.clean_title(self._need_text(ctx, "title", title))
        content = store.clean_content(title, content)
        ref = Ref(self._need_text(ctx, "path", path))
        if ref.has_hash:
            raise KbError("invalid_input", ctx.t("need_no_section", path=ref.raw))
        hit = self.lookup(ref.parts)
        if hit and hit[0] == "article":
            return await self._write_head(ctx, ref, self.articles[hit[1]], title, content, who)
        if hit and hit[0] == "topic":
            raise KbError("not_allowed", ctx.t("exists_other_kind", path=hit[1], kind=ctx.t("kind_topic")),
                          ctx.t("hint_call", call=ctx.call("get", hit[1])))
        pparts = ref.parts[:-1]
        if not pparts:
            raise KbError("not_allowed", ctx.t("article_in_root"),
                          ctx.t("hint_call", call=ctx.call("get", "")),
                          self._items(self.child_topics(""), "topic")[:ctx.limit])
        if len(pparts) > MAX_DEPTH:
            opts = self._level3_on(pparts)
            tgt = opts[0]["path"] if opts else "<topic>"
            raise KbError("too_deep", ctx.t("too_deep"),
                          ctx.t("hint_too_deep", call=ctx.call("save_article", tgt + "/" + norm_name(ref.parts[-1]), ..., ...)),
                          opts)
        ph = self.lookup(pparts)
        if not ph or ph[0] != "topic":
            self._missing_parent(ctx, pparts, "save_topic")
        parent = ph[1]
        if "/" not in parent:
            kids = self._items(self.child_topics(parent), "topic")
            tgt = kids[0]["path"] if kids else parent + "/<topic>"
            raise KbError("not_allowed", ctx.t("article_in_domain"),
                          ctx.t("hint_article_in_domain",
                                call=ctx.call("save_article", tgt + "/" + norm_name(ref.parts[-1]), ..., ...)),
                          self._rank(ref.obj, kids, ctx.limit))
        name = self._check_new_name(ctx, ref.parts[-1])
        newp = parent + "/" + name
        nb = store.new_nb(1, title, content)
        store.touch(nb.cells[0], who, who == "indexer")
        await self._write(self.file_of("article", newp), nb, who)
        self._note(ctx, ref, newp)
        return {"kind": "write", "path": newp, "action": "created"}

    async def save_section(self, ctx, path, title, content, who="mcp"):
        title = store.clean_title(self._need_text(ctx, "title", title))
        content = store.clean_content(title, content)
        ref = Ref(self._need_text(ctx, "path", path))
        hit = self.lookup(ref.parts)
        if not ref.sid:
            if hit and hit[0] != "root":
                raise KbError("invalid_input", ctx.t("need_section", path=ref.raw),
                              ctx.t("hint_need_section", call=ctx.call("get", hit[1]), path=hit[1]))
            raise KbError("invalid_input", ctx.t("need_section", path=ref.raw),
                          ctx.t("hint_call", call=ctx.search_call(ref.parts[-1] if ref.parts else "...")))
        if hit is None or hit[0] == "root":
            if len(ref.parts) >= 3 and self.lookup(ref.parts[:-1]):
                create = ctx.call("save_article", ref.obj, ..., ...)
                near = self.lookup(ref.parts[:-1])[1]
                items = self._items(self.child_articles(near), "article")
                raise KbError("not_found", ctx.t("article_parent_missing", path=ref.obj),
                              ctx.t("hint_article_parent", call=ctx.call("get", near), create=create),
                              self._rank(ref.obj, items, ctx.limit))
            self._not_found_obj(ctx, ref)
        doc = self.doc_of(*hit)
        sec = doc.find(ref.sid) or doc.find(norm_name(ref.sid))
        if sec is None:
            sid = norm_name(ref.sid)
            if not NAME_RE.match(sid) or sid == "head":
                raise KbError("invalid_name", ctx.t("invalid_id", name=ref.sid),
                              ctx.t("hint_retry_name"), [doc.path + "#" + suggest_name(ref.sid)])
        else:
            sid = sec.id
            if not sec.visible:
                raise KbError("not_allowed", ctx.t("untitled", path=sec.path),
                              ctx.t("hint_untitled", call=ctx.call("get", doc.path)))
        if doc.kind == "topic" and sid != "head":
            raise KbError("not_allowed", ctx.t("topic_one_section"),
                          ctx.t("hint_call", call=ctx.call("get", doc.path)))
        for s in doc.visible():
            if s.id != sid and s.title.casefold() == title.casefold():
                raise KbError("duplicate_title", ctx.t("duplicate_title", title=title),
                              ctx.t("hint_duplicate", call=ctx.call("save_section", s.path, ..., ...)),
                              [{"path": s.path, "title": s.title}])
        level = 1 if sid == "head" else 2
        src = store.build_source(level, title, content)
        newpath = doc.path + "#" + sid
        if sec is not None:
            if store.src_of(sec.cell) == src:
                self._note(ctx, ref, newpath)
                return {"kind": "write", "path": newpath, "action": "updated", "unchanged": True}
            sec.cell["source"] = src
            cell, action = sec.cell, "updated"
        else:
            cell = store.new_cell(src, sid)
            doc.nb.cells.append(cell)
            action = "created"
        store.touch(cell, who, who == "indexer")
        await self._save(doc, who)
        self._note(ctx, ref, newpath)
        return {"kind": "write", "path": newpath, "action": action}

    async def replace_lines(self, ctx, path, old, new, who="mcp"):
        """Replace a block of whole lines in a section's content (KB api#replace-lines).

        `old` must match exactly one run of consecutive lines (trailing spaces ignored);
        `new` replaces it, empty `new` deletes it. Several matches -> error, nothing written.
        """
        old_l = text_lines(self._need_text(ctx, "old", old))
        new_l = text_lines(new)
        ref = Ref(self._need_text(ctx, "path", path))
        if not ref.sid:
            hit = self.lookup(ref.parts)
            p = hit[1] if hit and hit[0] != "root" else ref.obj
            raise KbError("invalid_input", ctx.t("need_section", path=ref.raw),
                          ctx.t("hint_need_existing_section", call=ctx.call("get", p)))
        kind, doc, sec = self.resolve(ctx, ref.raw)
        lines = sec.content.split("\n") if sec.content else []
        want = [x.rstrip() for x in old_l]
        have = [x.rstrip() for x in lines]
        n = len(want)
        found = [i for i in range(len(have) - n + 1) if have[i:i + n] == want]
        if not found:
            self._old_not_found(ctx, sec, old_l, lines)
        if len(found) > 1:
            opts = []
            for i in found:
                before = lines[i - 1].strip() if i > 0 else ""
                after = lines[i + n].strip() if i + n < len(lines) else ""
                opts.append(ctx.t("match_at", line=i + 1, before=before[:80], after=after[:80]))
            raise KbError("ambiguous", ctx.t("old_ambiguous", count=len(found), path=sec.path),
                          ctx.t("hint_ambiguous", call=ctx.call("save_section", sec.path, sec.title, ...)),
                          opts)
        i = found[0]
        content = "\n".join(lines[:i] + new_l + lines[i + n:])
        src = store.build_source(sec.level, sec.title, content)
        if store.src_of(sec.cell) == src:
            return {"kind": "write", "path": sec.path, "action": "updated", "unchanged": True}
        sec.cell["source"] = src
        store.touch(sec.cell, who, who == "indexer")
        await self._save(doc, who)
        return {"kind": "write", "path": sec.path, "action": "updated"}

    def _old_not_found(self, ctx, sec, old_l, lines):
        hint_get = ctx.call("get", sec.path)
        part = old_l[0].strip() if len(old_l) == 1 else ""
        if part:
            inside = [x for x in lines if part in x]
            if inside:  # a piece of a line instead of the whole line
                raise KbError("not_found", ctx.t("old_partial", path=sec.path),
                              ctx.t("hint_old_partial"), [x.strip()[:200] for x in inside[:ctx.limit]])
        first = next((x for x in old_l if x.strip()), "")
        near = difflib.get_close_matches(first.strip(), [x.strip() for x in lines if x.strip()],
                                         n=min(3, ctx.limit), cutoff=0.5)
        raise KbError("not_found", ctx.t("old_not_found", path=sec.path),
                      ctx.t("hint_old_not_found", call=hint_get), [x[:200] for x in near])

    def _tag_target(self, ctx, path):
        kind, doc, sec = self.resolve(ctx, self._need_text(ctx, "path", path))
        if kind != "section":
            raise KbError("invalid_input", ctx.t("need_section", path=str(path)),
                          ctx.t("hint_need_section", call=ctx.call("get", doc.path if doc else ""),
                                path=doc.path if doc else ""))
        if sec.id == "head":
            raise KbError("not_allowed", ctx.t("head_tags"),
                          ctx.t("hint_call", call=ctx.call("get", doc.path)))
        return doc, sec

    async def add_section_tags(self, ctx, path, tags, who="mcp"):
        new = as_list(self._need_text(ctx, "tags", tags))
        doc, sec = self._tag_target(ctx, path)
        cur = sec.meta.setdefault("tags", [])
        have = {tg["text"].casefold() for tg in cur}
        added = False
        for tg in new:
            if tg.casefold() not in have:
                cur.append({"text": tg, "source": who})
                have.add(tg.casefold())
                added = True
        if not added:
            return {"kind": "write", "path": sec.path, "action": "updated", "unchanged": True}
        store.touch(sec.cell, who, who == "indexer")
        await self._save(doc, who)
        return {"kind": "write", "path": sec.path, "action": "updated"}

    async def drop_section_tags(self, ctx, path, tags, who="mcp"):
        drop = {x.casefold() for x in as_list(self._need_text(ctx, "tags", tags))}
        doc, sec = self._tag_target(ctx, path)
        cur = sec.meta.get("tags") or []
        keep = [tg for tg in cur if tg["text"].casefold() not in drop]
        if len(keep) == len(cur):
            return {"kind": "write", "path": sec.path, "action": "updated", "unchanged": True}
        sec.meta["tags"] = keep
        store.touch(sec.cell, who, who == "indexer")
        await self._save(doc, who)
        return {"kind": "write", "path": sec.path, "action": "updated"}

    async def delete(self, ctx, path, who="mcp"):
        kind, doc, sec = self.resolve(ctx, path or "")
        if kind == "root":
            raise KbError("not_allowed", ctx.t("delete_root"), "")
        if kind == "section":
            if sec.id == "head":
                raise KbError("not_allowed", ctx.t("delete_head"),
                              ctx.t("hint_delete_head", call=ctx.call("delete", doc.path)),
                              [{"path": doc.path, "title": doc.title}])
            doc.nb.cells.remove(sec.cell)
            await self._save(doc, who)
            return {"kind": "write", "path": sec.path, "action": "deleted"}
        if kind == "article":
            await ensure_async(self.cm.delete(doc.file))
            self._drop(doc.file, who)
            self._rekey()
            return {"kind": "write", "path": doc.path, "action": "deleted"}
        # topic: only empty
        d = doc.file.rsplit("/", 1)[0]
        kids = self._items(self.child_topics(doc.path), "topic") + \
            self._items(self.child_articles(doc.path), "article")
        listing = await self._get(d, content=True, type="directory")
        other = sorted(e["name"] for e in listing["content"]
                       if not e["name"].startswith(".") and e["name"] != "topic.ipynb")
        if kids or other:
            raise KbError("not_empty", ctx.t("not_empty", path=doc.path), ctx.t("hint_not_empty"),
                          kids or [doc.path + "/" + x for x in other])
        await ensure_async(self.cm.delete(doc.file))
        self._drop(doc.file, who)
        await ensure_async(self.cm.delete(d))
        self._rekey()
        return {"kind": "write", "path": doc.path, "action": "deleted"}

    # ---------------------------------------------------------------- admin
    def unindexed(self, ctx, path=None):
        items = []
        for d in self.scope(ctx, path):
            secs = d.sections()
            bad = [s for s in secs if not s.meta.get("indexed")]
            nohead = d.head() is None or (secs and secs[0].id != "head")
            if bad or nohead:
                oldest = min((s.meta.get("updated", "") for s in bad), default="")
                items.append({"path": d.path, "unindexed": len(bad), "oldest": oldest,
                              "no_head": bool(nohead)})
        items.sort(key=lambda x: (x["oldest"] or "~", x["path"]))
        return {"kind": "unindexed", "items": items}

    def get_full(self, ctx, path):
        kind, doc, sec = self.resolve(ctx, self._need_text(ctx, "path", path), admin=True)
        if kind == "root":
            raise KbError("invalid_input", ctx.t("nf_article", path=""),
                          ctx.t("hint_call", call=ctx.call("unindexed")))
        return {"kind": "full", "path": doc.path, "version": doc.version,
                "sections": [self.sec_full(s) for s in doc.sections()]}

    def _parse_sections(self, ctx, sections):
        if isinstance(sections, str):
            import json
            try:
                sections = json.loads(sections)
            except ValueError as e:
                raise KbError("invalid_input", ctx.t("bad_sections", err=str(e)))
        if not isinstance(sections, list) or not sections:
            raise KbError("invalid_input", ctx.t("bad_sections", err="empty or not a list"))
        out, seen = [], set()
        for i, s in enumerate(sections):
            if not isinstance(s, dict):
                raise KbError("invalid_input", ctx.t("bad_sections", err="item %d is not an object" % i))
            sid = norm_name(str(s.get("id") or ""))
            if not NAME_RE.match(sid):
                raise KbError("invalid_name", ctx.t("invalid_id", name=s.get("id")), ctx.t("hint_retry_name"),
                              [suggest_name(str(s.get("id") or "section"))])
            if sid in seen:
                raise KbError("invalid_input", ctx.t("dup_id", id=sid))
            seen.add(sid)
            # title / content omitted (None) = keep the current ones of this id (filled in _keep_text)
            title = s.get("title")
            content = s.get("content")
            if title is not None:
                title = store.clean_title(title)
            if content is not None:
                content = store.clean_content(title, content) if title else str(content).rstrip()
            tags = s.get("tags")
            if tags is not None:
                if isinstance(tags, str):
                    tags = as_list(tags)
                tags = store._norm_tags([{"text": x, "source": "indexer"} if isinstance(x, str) else x
                                         for x in tags])
            out.append({"id": sid, "title": title, "content": content, "tags": tags})
        return out

    @staticmethod
    def _keep_text(ctx, secs, doc):
        """Omitted title / content -> current values of the section with this id (tags-only writes)."""
        cur = {s.id: s for s in doc.sections()}
        for s in secs:
            if s["title"] is not None and s["content"] is not None:
                continue
            old = cur.get(s["id"])
            if old is None:
                raise KbError("invalid_input", ctx.t("need_text", id=s["id"]))
            if s["title"] is None:
                s["title"] = old.title or ""
            if s["content"] is None:
                s["content"] = old.content if old.visible else store.src_of(old.cell).rstrip()
            if s["title"] and s["content"] is not None:
                s["content"] = store.clean_content(s["title"], s["content"])

    def _check_titles(self, ctx, secs):
        seen = {}
        for s in secs:
            if not s["title"]:
                continue
            k = s["title"].casefold()
            if k in seen:
                raise KbError("duplicate_title", ctx.t("duplicate_title", title=s["title"]), "",
                              [{"path": "#" + seen[k], "title": s["title"]}])
            seen[k] = s["id"]

    def _admin_doc(self, ctx, path, version):
        ref = Ref(self._need_text(ctx, "path", path))
        hit = self.lookup(ref.parts)
        if hit is None or hit[0] == "root" or ref.sid:
            self._not_found_obj(ctx, Ref(ref.obj))
        doc = self.doc_of(*hit)
        self._note(ctx, ref, doc.path)
        if str(version or "") != doc.version:
            raise KbError("conflict", ctx.t("conflict", path=doc.path, current=doc.version),
                          ctx.t("hint_conflict", call=ctx.call("get_full", doc.path)))
        return doc

    @staticmethod
    def _make_cell(s, old_cell):
        if s["title"]:
            src = store.build_source(1 if s["id"] == "head" else 2, s["title"], s["content"])
        else:
            src = s["content"]
        cell = old_cell if old_cell is not None else store.new_cell(src, s["id"])
        cell["source"] = src
        kb = store.kbmeta(cell)
        kb["id"] = s["id"]
        if s["tags"] is not None:
            kb["tags"] = s["tags"]
        store.touch(cell, "indexer", True)
        return cell

    async def put_article(self, ctx, path, sections, version):
        secs = self._parse_sections(ctx, sections)
        doc = self._admin_doc(ctx, path, version)
        self._keep_text(ctx, secs, doc)
        if secs[0]["id"] != "head" or not secs[0]["title"]:
            raise KbError("invalid_input", ctx.t("need_head"))
        if doc.kind == "topic" and len(secs) != 1:
            raise KbError("invalid_input", ctx.t("topic_one_section"))
        self._check_titles(ctx, secs)
        old = {s.id: s.cell for s in doc.sections()}
        doc.nb.cells = [self._make_cell(s, old.get(s["id"])) for s in secs]
        await self._save(doc, "indexer")
        return {"kind": "write", "path": doc.path, "action": "updated",
                "version": self.doc_by_file(doc.file).version}

    async def put_sections(self, ctx, path, sections, version):
        secs = self._parse_sections(ctx, sections)
        doc = self._admin_doc(ctx, path, version)
        self._keep_text(ctx, secs, doc)
        cur = {s.id: s for s in doc.sections()}
        merged = []
        for s in doc.sections():
            merged.append({"id": s.id, "title": s.title or ""})
        for s in secs:
            for m in merged:
                if m["id"] == s["id"]:
                    m["title"] = s["title"]
                    break
            else:
                merged.append({"id": s["id"], "title": s["title"]})
        if doc.kind == "topic" and len(merged) != 1:
            raise KbError("invalid_input", ctx.t("topic_one_section"))
        self._check_titles(ctx, merged)
        for s in secs:
            old = cur.get(s["id"])
            cell = self._make_cell(s, old.cell if old else None)
            if old is None:
                doc.nb.cells.append(cell)
        await self._save(doc, "indexer")
        return {"kind": "write", "path": doc.path, "action": "updated",
                "version": self.doc_by_file(doc.file).version}


def text_lines(v):
    """Lines of an `old` / `new` argument: a string (lenient like content) or a list of lines.
    One trailing newline is ignored; empty -> no lines (deletion)."""
    if v is None:
        return []
    if isinstance(v, list):
        s = "\n".join(str(x) for x in v)
    else:
        s = str(v).replace("\r\n", "\n")
        if "\n" not in s and "\\n" in s:
            s = s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    if s.endswith("\n"):
        s = s[:-1]
    return s.split("\n") if s else []


def as_subs(v):
    """Search substrings: a list, a JSON list in a string, or one plain substring."""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            import json
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    v = parsed
            except ValueError:
                pass
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if v is not None and str(v).strip() else []


def as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            import json
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except ValueError:
                pass
        return [x.strip() for x in s.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(as_list(x) if isinstance(x, str) and "," in x else [str(x).strip()])
        return [x for x in out if x]
    return [str(v).strip()]
