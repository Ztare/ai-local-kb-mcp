"""Notebook storage: sections in Markdown cells, metadata in cell.metadata.kb (KB storage#format, storage#metadata)."""
import copy
import datetime
import hashlib
import re
import secrets

import nbformat

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
NAME_RE = re.compile(r"^[a-z0-9-]+$")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_hash(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def bytes_hash(b):
    return hashlib.sha256(b).hexdigest()[:16]


def src_of(cell):
    s = cell.get("source", "")
    return "".join(s) if isinstance(s, list) else (s or "")


def parse_source(src):
    """-> (title or None, level, content). Heading is only the first line."""
    first, _, rest = src.partition("\n")
    m = HEADING_RE.match(first.rstrip("\r"))
    if not m:
        return None, 0, src
    title = m.group(2).rstrip("#").strip()
    if not title:
        return None, 0, src
    return title, len(m.group(1)), rest.lstrip("\r\n").rstrip()


def build_source(level, title, content):
    content = (content or "").strip("\r\n").rstrip()
    head = "#" * level + " " + title
    return head + ("\n\n" + content if content else "")


def clean_title(title):
    t = " ".join(str(title).split())
    return re.sub(r"^#+\s*", "", t).strip()


def clean_content(title, content):
    """Lenient content: real newlines, no duplicated heading line."""
    s = str(content if content is not None else "").replace("\r\n", "\n")
    if "\n" not in s and ("\\n" in s):
        s = s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    s = s.lstrip("\n")
    first, _, rest = s.partition("\n")
    m = HEADING_RE.match(first)
    if m and title and m.group(2).rstrip("#").strip().casefold() == title.casefold():
        s = rest.lstrip("\n")
    return s.rstrip()


def kbmeta(cell, create=True):
    md = cell.get("metadata")
    if md is None:
        cell["metadata"] = {}
        md = cell["metadata"]
    kb = md.get("kb")
    if not isinstance(kb, dict):
        if not create:
            return None
        md["kb"] = {}
        kb = md["kb"]  # NotebookNode may copy the dict on assignment
    return kb


def new_id(taken):
    while True:
        cid = secrets.token_hex(3)[:5]
        if cid not in taken and not cid.isdigit():
            return cid


def fill_kernel_meta(nb, default):
    """MCP / indexer writes only: a notebook without kernelspec gets the server's default one
    (KB storage#format). An existing kernelspec is never touched. Returns True if filled."""
    if not default:
        return False
    meta = nb.get("metadata")
    if not isinstance(meta, dict):
        meta = nb["metadata"] = {}
    if meta.get("kernelspec"):
        return False
    for k, v in default.items():
        meta.setdefault(k, copy.deepcopy(v))
    return True


def ensure_meta(nb, who):
    """Bring cells to our rules in place (KB storage#metadata, storage#lifecycle). Returns True if anything changed.

    Works on NotebookNode and on plain dicts (web pre-save model).
    """
    changed = False
    now = now_iso()
    # Notebook-level metadata (kernelspec...) belongs to JupyterLab and the user: never touched here,
    # only ignored by version / hashes (KB storage#format).
    cells = nb.get("cells", [])
    for i, cell in enumerate(cells):
        if cell.get("cell_type") != "markdown":
            src = src_of(cell)
            if cell.get("cell_type") == "code" and src.strip():
                src = "```python\n" + src.rstrip() + "\n```"
            keep = {k: cell[k] for k in ("id", "metadata") if k in cell}
            cell.clear()
            cell.update(keep)
            cell["cell_type"] = "markdown"
            cell["source"] = src
            cell.setdefault("metadata", {})
            changed = True
        if isinstance(cell.get("source"), list):
            cell["source"] = src_of(cell)
        if kbmeta(cell, create=False) is None:
            changed = True
        kb = kbmeta(cell)
        h = text_hash(src_of(cell))
        if kb.get("hash") != h:
            kb["hash"] = h
            kb["indexed"] = False
            kb["updated"] = now
            kb["changed_by"] = who
            changed = True
        if "indexed" not in kb:
            kb["indexed"] = False
            changed = True
        tags = kb.get("tags")
        if tags is not None:
            norm = _norm_tags(tags)
            if norm != tags:
                kb["tags"] = norm
                changed = True
    # ids: valid, unique; first heading cell may become head
    ids = [kbmeta(c).get("id") for c in cells]
    taken = {x for x in ids if isinstance(x, str)}
    seen = set()
    for i, cell in enumerate(cells):
        kb = kbmeta(cell)
        cid = kb.get("id")
        if isinstance(cid, str) and NAME_RE.match(cid) and cid not in seen:
            seen.add(cid)
            continue
        title, level, _ = parse_source(src_of(cell))
        if i == 0 and level == 1 and "head" not in taken:
            cid = "head"
        else:
            cid = new_id(taken | seen)
        kb["id"] = cid
        taken.add(cid)
        seen.add(cid)
        changed = True
    return changed


def _norm_tags(tags):
    out, seen = [], set()
    if not isinstance(tags, list):
        return out
    for tg in tags:
        if isinstance(tg, str):
            tg = {"text": tg, "source": "web"}
        if not isinstance(tg, dict) or not str(tg.get("text", "")).strip():
            continue
        text = str(tg["text"]).strip()
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append({"text": text, "source": tg.get("source") or "web"})
    return out


class Sec:
    """View of one cell as a section."""

    __slots__ = ("doc", "cell", "id", "title", "level", "content", "meta")

    def __init__(self, doc, cell):
        self.doc = doc
        self.cell = cell
        self.meta = kbmeta(cell)
        self.id = self.meta.get("id")
        self.title, self.level, self.content = parse_source(src_of(cell))

    @property
    def path(self):
        return self.doc.path + "#" + self.id

    @property
    def tags(self):
        return [tg["text"] for tg in self.meta.get("tags") or []]

    @property
    def visible(self):
        return self.title is not None


class Doc:
    """One notebook: an article or a topic head file."""

    def __init__(self, kind, path, file, nb, version):
        self.kind = kind  # 'article' | 'topic'
        self.path = path
        self.file = file  # API path in the ContentsManager
        self.nb = nb
        self.version = version

    def sections(self):
        return [Sec(self, c) for c in self.nb.cells]

    def visible(self):
        return [s for s in self.sections() if s.visible]

    def find(self, sid):
        k = sid.casefold()
        for s in self.sections():
            if s.id and s.id.casefold() == k:
                return s
        return None

    def head(self):
        return self.find("head")

    @property
    def name(self):
        return self.path.rsplit("/", 1)[-1]

    @property
    def title(self):
        h = self.head()
        return h.title if h and h.title else self.name


def new_nb(level, title, content, sid="head"):
    nb = nbformat.v4.new_notebook()
    nb.cells.append(nbformat.v4.new_markdown_cell(build_source(level, title, content)))
    kbmeta(nb.cells[0])["id"] = sid
    return nb


def new_cell(source, sid):
    c = nbformat.v4.new_markdown_cell(source)
    kbmeta(c)["id"] = sid
    return c


def touch(cell, who, indexed):
    """Mark a cell as written now by `who`."""
    kb = kbmeta(cell)
    src = src_of(cell)
    h = text_hash(src)
    if kb.get("hash") != h or "updated" not in kb:
        kb["updated"] = now_iso()
    kb["hash"] = h
    kb["indexed"] = indexed
    kb["changed_by"] = who
