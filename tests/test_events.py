"""Index follows the server: save hooks + contents events. Rescan (refresh) is forbidden here."""
import asyncio

from jupyter_server.services.contents.filemanager import AsyncFileContentsManager

from kb_mcp.hooks import attach
from kb_mcp.kb import KB, Ctx

P = "kb/coding/python/asyncio.ipynb"


async def make(tmp_path):
    cm = AsyncFileContentsManager(root_dir=str(tmp_path), delete_to_trash=False)
    kb = KB(cm)
    attach(kb, cm)
    await kb.refresh()
    c = Ctx()
    await kb.save_topic(c, "coding", "Код", "x")
    await kb.save_topic(c, "coding/python", "Python", "x")
    await kb.save_article(c, "coding/python/asyncio", "Asyncio", "x")
    await kb.save_section(c, "coding/python/asyncio#event-loop", "Event loop", "old text")
    await asyncio.sleep(0.05)

    async def no_rescan():
        raise AssertionError("rescan must not be needed")
    kb.refresh = no_rescan
    return kb, cm


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(coro)


def paths(kb, topic):
    return [x["path"] for x in kb.get(Ctx(), topic)["articles"]]


def test_web_save_updates_index_at_once(tmp_path):
    async def go():
        kb, cm = await make(tmp_path)
        model = await cm.get(P)
        nb = model["content"]
        nb["cells"][1]["source"] = "## Event loop\n\nnew text from web"
        nb["cells"].append({"cell_type": "markdown", "id": "jl-1", "metadata": {}, "source": "## Web section\n\nhello"})
        nb["cells"].append({"cell_type": "code", "id": "jl-2", "metadata": {}, "source": "x = 1",
                            "outputs": [], "execution_count": None})
        await cm.save({"type": "notebook", "format": "json", "content": nb}, P)
        # no settle(): the post-save hook has already indexed it
        c = Ctx()
        assert [x["path"] for x in kb.search(c, "new text from web")["items"]] == ["coding/python/asyncio#event-loop"]
        secs = kb.get_full(c, "coding/python/asyncio")["sections"]
        assert secs[1]["changed_by"] == "web" and not secs[1]["indexed"]
        assert secs[2]["title"] == "Web section" and secs[2]["changed_by"] == "web"
        assert secs[3]["content"] == "```python\nx = 1\n```"
        web_id = secs[2]["id"]
        # the browser saves again with its stale model (no kb metadata on its new cell)
        nb["cells"][2]["source"] = "## Web section\n\nhello again"
        await cm.save({"type": "notebook", "format": "json", "content": nb}, P)
        secs = kb.get_full(c, "coding/python/asyncio")["sections"]
        assert secs[2]["id"] == web_id and secs[2]["content"] == "hello again"
    run(go())


def test_mcp_write_goes_through_hooks(tmp_path):
    async def go():
        kb, cm = await make(tmp_path)
        seen = []
        cm.register_pre_save_hook(lambda model, path, **kw: seen.append(path))
        await kb.save_section(Ctx(), "coding/python/asyncio#tasks", "Tasks", "t")
        assert seen == [P]
        m = kb.get_full(Ctx(), "coding/python/asyncio")["sections"][-1]
        assert m["changed_by"] == "mcp" and m["id"] == "tasks"
    run(go())


def test_rename_delete_copy_events(tmp_path):
    async def go():
        kb, cm = await make(tmp_path)
        await cm.rename(P, "kb/coding/python/aio.ipynb")
        await settle()
        assert paths(kb, "coding/python") == ["coding/python/aio"]
        assert kb.search(Ctx(), "old text")["items"][0]["path"] == "coding/python/aio#event-loop"

        await cm.copy("kb/coding/python/aio.ipynb", "kb/coding/python")
        await settle()
        assert len(paths(kb, "coding/python")) == 2

        await cm.delete("kb/coding/python/aio.ipynb")
        await settle()
        assert "coding/python/aio" not in paths(kb, "coding/python")

        await cm.rename("kb/coding/python", "kb/coding/py")
        await settle()
        assert [x["path"] for x in kb.get(Ctx(), "coding")["topics"]] == ["coding/py"]
        assert len(paths(kb, "coding/py")) == 1

        await cm.save({"type": "directory"}, "kb/coding/rust")
        await settle()
        assert kb.get(Ctx(), "coding/rust")["head"]["content"] == "без описания"
    run(go())


def test_new_notebook_from_web(tmp_path):
    async def go():
        kb, cm = await make(tmp_path)
        nb = {"cells": [{"cell_type": "markdown", "id": "a", "metadata": {}, "source": "# Fresh\n\nmade in web"}],
              "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        await cm.save({"type": "notebook", "format": "json", "content": nb}, "kb/coding/python/fresh.ipynb")
        assert kb.get(Ctx(), "coding/python/fresh")["head"]["title"] == "Fresh"
    run(go())
