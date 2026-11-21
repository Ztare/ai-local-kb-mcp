"""kb_mcp - knowledge base MCP plugin for Jupyter Server.

Docs live in the knowledge base itself: start at development/mcp-kb/overview.
Refs like "KB storage#git" mean development/mcp-kb/storage#git."""
import atexit
import os

__version__ = "0.1.0"


def _jupyter_server_extension_points():
    return [{"module": "kb_mcp"}]


def _load_jupyter_server_extension(serverapp):
    from jupyter_server.utils import url_path_join
    from tornado.ioloop import IOLoop

    from .hooks import attach
    from .kb import KB
    from .launch import default_kernel_meta, print_settings, setup_token
    from .mcp import Docs, DocsHandler, McpHandler

    log = serverapp.log
    token_path = setup_token(serverapp, log)  # KB setup#token
    cm = serverapp.contents_manager
    kb = KB(cm, root="kb", log=log)
    kb.default_nb_meta = None  # filled at startup from the default kernel (KB storage#format)
    attach(kb, cm, log)
    docs = Docs(cm, "mcp_docs")

    # Git works on real files only (KB storage#git): with a non-file ContentsManager it is off.
    git = None
    root_dir = getattr(cm, "root_dir", None)
    if root_dir and os.path.isdir(root_dir):
        from .git import AutoCommit
        loop = IOLoop.current()
        kb_dir = os.path.join(os.path.abspath(root_dir), "kb")
        os.makedirs(kb_dir, exist_ok=True)
        git = AutoCommit(kb_dir, delay=float(os.environ.get("KB_MCP_COMMIT_DELAY", "180")),
                         call_later=lambda d, cb: loop.call_later(d, cb), log=log)
        atexit.register(git.commit_now)
    else:
        log.warning("kb-mcp: contents manager has no root_dir - git autocommit is off")

    async def startup():
        # asks the kernel's interpreter in a subprocess - off the event loop
        kb.default_nb_meta = await IOLoop.current().run_in_executor(None, default_kernel_meta, serverapp, log)
        async with kb.lock:
            await kb.refresh()
        if git:
            git.commit_now()  # offline edits since the last run
            kb.on_change = git.notify
        log.info("kb-mcp: %d topics, %d articles", len(kb.topics), len(kb.articles))
        print_settings(serverapp, token_path)  # after bind: the real port

    IOLoop.current().add_callback(startup)

    base = serverapp.web_app.settings["base_url"]
    serverapp.web_app.add_handlers(".*$", [
        (url_path_join(base, "mcp/kb"), McpHandler, {"kb": kb, "docs": docs}),
        (url_path_join(base, "mcp/kb-admin"), McpHandler, {"kb": kb, "docs": docs, "admin": True}),
        (url_path_join(base, r"mcp/docs/(en|ru)/(lite|std|full|admin)(?:/([a-z0-9-]+))?"), DocsHandler, {"docs": docs}),    ])
    serverapp.web_app.settings["kb_mcp"] = kb
