"""Server integration: pre/post-save hooks and contents events keep metadata and the index right (KB storage#lifecycle, plugin#hooks).

Every save goes through the same hooks - ours (MCP / indexer) and the web's.
Who saves is taken from kb.WRITER (set by our writes; default 'web').
"""
import copy

from . import store
from .kb import WRITER


def attach(kb, cm, log=None):
    def pre_save(model=None, path=None, contents_manager=None, **kw):
        if not model or model.get("type") != "notebook" or not path:
            return
        p = path.strip("/")
        if kb._classify(p) is None:
            return
        nb = model.get("content")
        if not isinstance(nb, dict):
            return
        # The browser does not get our metadata back after a save, so cells it created
        # arrive without `kb` every time: take it from the indexed copy by Jupyter cell id.
        doc = kb.doc_by_file(p)
        if doc is not None:
            known = {c.get("id"): (c.get("metadata") or {}).get("kb") for c in doc.nb.cells}
            for c in nb.get("cells", []):
                md = c.setdefault("metadata", {})
                k = known.get(c.get("id"))
                if not isinstance(md.get("kb"), dict) and isinstance(k, dict):
                    md["kb"] = copy.deepcopy(dict(k))
        store.ensure_meta(nb, WRITER.get())
        if WRITER.get() in ("mcp", "indexer"):  # web saves carry JupyterLab's own metadata - never touched
            store.fill_kernel_meta(nb, getattr(kb, "default_nb_meta", None))
        kb.stash[p] = nb

    def post_save(model=None, os_path=None, contents_manager=None, **kw):
        p = ((model or {}).get("path") or "").strip("/")
        nb = kb.stash.pop(p, None)
        if nb is not None:
            kb.index_nb(p, nb, model.get("last_modified"), WRITER.get())

    async def on_contents_event(logger=None, schema_id=None, data=None):
        data = data or {}
        if data.get("action") in (None, "get"):
            return
        try:
            async with kb.lock:
                await kb.on_event(data["action"], data.get("path"), data.get("source_path"))
        except Exception:
            if log:
                log.exception("kb-mcp: contents event failed: %s", data)

    cm.register_pre_save_hook(pre_save)
    cm.register_post_save_hook(post_save)
    cm.event_logger.add_listener(schema_id=cm.event_schema_id, listener=on_contents_event)
    return pre_save, post_save, on_contents_event
