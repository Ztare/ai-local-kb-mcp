import asyncio
import functools
import os
import time

import nbformat
import pytest
from jupyter_server.services.contents.filemanager import AsyncFileContentsManager

from kb_mcp import render, store
from kb_mcp.hooks import attach
from kb_mcp.kb import KB, Ctx, KbError


async def make(tmp_path):
    cm = AsyncFileContentsManager(root_dir=str(tmp_path), delete_to_trash=False)
    k = KB(cm)
    attach(k, cm)
    await k.refresh()
    c = Ctx()
    await k.save_topic(c, "coding", "Программирование", "Всё про код")
    await k.save_topic(c, "coding/python", "Python", "Язык Python")
    await k.save_article(c, "coding/python/asyncio", "Asyncio", "Асинхронность в Python")
    await k.save_section(c, "coding/python/asyncio#event-loop", "Event loop", "Цикл событий.\n\n```py\nloop.run()\n```")
    await k.save_section(c, "coding/python/asyncio#tasks", "Задачи", "Создание задач: create_task")
    await k.add_section_tags(c, "coding/python/asyncio#event-loop", "event loop, concurrency")
    k.disk = str(tmp_path / "kb")
    await asyncio.sleep(0.05)  # let contents events of the setup settle
    return k


def aio(fn):
    @functools.wraps(fn)
    def wrapper(tmp_path):
        async def go():
            kb = await make(tmp_path)
            await fn(kb)
        asyncio.run(go())
    wrapper.__signature__ = __import__("inspect").Signature(
        [__import__("inspect").Parameter("tmp_path", __import__("inspect").Parameter.POSITIONAL_OR_KEYWORD)])
    return wrapper


async def err(fn, *a, **kw):
    with pytest.raises(KbError) as e:
        r = fn(*a, **kw)
        if asyncio.iscoroutine(r):
            await r
    return e.value

@aio
async def test_tree_and_get(kb):
    c = Ctx()
    r = kb.get(c, "")
    assert [x["path"] for x in r["topics"]] == ["coding"]
    r = kb.get(c, "coding/python")
    assert r["articles"][0] == {"path": "coding/python/asyncio", "title": "Asyncio"}
    r = kb.get(c, "coding/python/asyncio")
    assert r["head"]["title"] == "Asyncio"
    assert [s["path"] for s in r["sections"]] == ["coding/python/asyncio#event-loop", "coding/python/asyncio#tasks"]
    r = kb.get(c, "coding/python/asyncio#event-loop", with_tags=True)
    assert r["section"]["content"].startswith("Цикл событий.")
    assert r["section"]["tags"] == ["event loop", "concurrency"]


@aio
async def test_file_layout(kb):
    f = os.path.join(kb.disk, "coding", "python", "asyncio.ipynb")
    nb = nbformat.read(f, 4)
    assert nb.cells[0].source.startswith("# Asyncio")
    assert nb.cells[1].source.startswith("## Event loop\n\n")
    m = nb.cells[1].metadata["kb"]
    assert m["id"] == "event-loop" and m["indexed"] is False and m["changed_by"] == "mcp"
    assert m["hash"] == store.text_hash(nb.cells[1].source)
    assert m["tags"][0] == {"text": "event loop", "source": "mcp"}


@aio
async def test_normalization(kb):
    c = Ctx()
    r = kb.get(c, " Coding/Python/asyncio.ipynb ")
    assert r["path"] == "coding/python/asyncio"
    assert c.normalized == "coding/python/asyncio"
    c = Ctx()
    kb.get(c, "coding/python/topic.ipynb")
    assert c.normalized == "coding/python"
    c = Ctx()
    kb.get(c, "coding/python/asyncio#Event_Loop")
    assert c.normalized == "coding/python/asyncio#event-loop"


@aio
async def test_not_found_options(kb):
    e = await err(kb.get, Ctx(), "coding/python/asyncio#evnt-loop")
    assert e.code == "not_found"
    assert e.options[0]["path"] == "coding/python/asyncio#event-loop"
    assert 'get("coding/python/asyncio")' in e.hint
    e = await err(kb.get, Ctx(), "coding/python/asynco")
    assert e.options[0]["path"] == "coding/python/asyncio"
    e = await err(kb.get, Ctx(profile="lite"), "coding/python/asynco")
    assert 'search("asynco")' in e.hint
    e = await err(kb.get, Ctx(), "cooking")
    assert e.options[0]["path"] == "coding"


@aio
async def test_search(kb):
    c = Ctx()
    r = kb.search(c, ["задач", "nothing"])
    assert [x["path"] for x in r["items"]] == ["coding/python/asyncio#tasks"]
    r = kb.search(c, "ЦИКЛ")
    assert r["items"][0]["path"] == "coding/python/asyncio#event-loop"
    r = kb.search(c, "a, b")  # one substring, commas are not separators
    assert r["items"] == []
    r = kb.search(c, "python", path="coding/python/asyncio")
    assert all(x["path"].startswith("coding/python/asyncio#") for x in r["items"])
    r = kb.search_regex(c, r"create_\w+")
    assert r["items"][0]["path"] == "coding/python/asyncio#tasks"
    assert (await err(kb.search_regex, c, "(")).code == "invalid_pattern"


@aio
async def test_tags(kb):
    c = Ctx()
    assert (await err(kb.section_tags, c, "")).code == "not_allowed"
    assert kb.section_tags(c, "coding")["tags"] == ["concurrency", "event loop"]
    assert kb.search_section_tags(c, ["conc"])["tags"] == ["concurrency"]
    r = kb.sections_by_tag(c, "Event Loop")
    assert r["sections"][0]["path"] == "coding/python/asyncio#event-loop"
    e = await err(kb.sections_by_tag, c, "loop")
    assert e.code == "not_found" and "event loop" in e.options
    assert (await kb.add_section_tags(c, "coding/python/asyncio#event-loop", ["event loop"])).get("unchanged")
    assert (await kb.drop_section_tags(c, "coding/python/asyncio#event-loop", "nope")).get("unchanged")
    await kb.drop_section_tags(c, "coding/python/asyncio#event-loop", "concurrency")
    assert kb.section_tags(c, "coding")["tags"] == ["event loop"]


@aio
async def test_write_rules(kb):
    c = Ctx()
    r = await kb.save_section(c, "coding/python/asyncio#tasks", "Задачи", "Новый текст")
    assert r["action"] == "updated"
    r = await kb.save_section(c, "coding/python/asyncio#tasks", "Задачи", "Новый текст")
    assert r.get("unchanged")
    e = await err(kb.save_section, c, "coding/python/asyncio#evnt-loop", "Event loop", "x")
    assert e.code == "duplicate_title" and e.options[0]["path"] == "coding/python/asyncio#event-loop"
    e = await err(kb.save_section, c, "coding/python/asyncio#Отмена задач", "Отмена", "x")
    assert e.code == "invalid_name" and e.options == ["coding/python/asyncio#otmena-zadach"]
    e = await err(kb.save_section, c, "coding/python/asyncio", "X", "x")
    assert e.code == "invalid_input"
    e = await err(kb.save_article, c, "coding/wheels", "W", "x")
    assert e.code == "not_allowed" and e.options[0]["path"] == "coding/python"
    await kb.save_topic(c, "coding/python/packaging", "Packaging", "x")
    e = await err(kb.save_topic, c, "coding/python/packaging/deep", "D", "x")
    assert e.code == "too_deep"
    e = await err(kb.save_article, c, "coding/python/packaging/deep/a", "A", "x")
    assert e.code == "too_deep" and e.options[0]["path"] == "coding/python/packaging"
    e = await err(kb.save_article, c, "coding/rust/borrow", "B", "x")
    assert e.code == "not_found"
    e = await err(kb.save_topic, c, "coding/Отладка", "Отладка", "x")
    assert e.code == "invalid_name" and e.options == ["otladka"]
    r = await kb.save_topic(c, "coding/My Topic", "Mine", "x")
    assert r["path"] == "coding/my-topic"


@aio
async def test_content_leniency(kb):
    c = Ctx()
    await kb.save_section(c, "coding/python/asyncio#gather", "Gather", "## Gather\n\nline1\\nline2")
    s = kb.get(c, "coding/python/asyncio#gather")["section"]
    assert s["content"] == "line1\\nline2"  # real newline present -> no unescape
    await kb.save_section(c, "coding/python/asyncio#wait", "Wait", "a\\nb \\\"q\\\"")
    assert kb.get(c, "coding/python/asyncio#wait")["section"]["content"] == 'a\nb "q"'


@aio
async def test_delete(kb):
    c = Ctx()
    assert (await err(kb.delete, c, "coding/python/asyncio#head")).code == "not_allowed"
    assert (await kb.delete(c, "coding/python/asyncio#tasks"))["action"] == "deleted"
    e = await err(kb.delete, c, "coding/python")
    assert e.code == "not_empty" and e.options[0]["path"] == "coding/python/asyncio"
    await kb.delete(c, "coding/python/asyncio")
    await kb.delete(c, "coding/python")
    assert not os.path.exists(os.path.join(kb.disk, "coding", "python"))
    assert kb.get(c, "coding")["topics"] == []


@aio
async def test_external_edit_detected(kb):
    """Edit past Jupyter: index picks it up on refresh, marks external."""
    f = os.path.join(kb.disk, "coding", "python", "asyncio.ipynb")
    nb = nbformat.read(f, 4)
    nb.cells[2].source = "## Задачи\n\nПереписано вручную: TaskGroup"
    time.sleep(0.01)
    nbformat.write(nb, f)
    await kb.refresh()
    c = Ctx()
    assert kb.search(c, "TaskGroup")["items"][0]["path"] == "coding/python/asyncio#tasks"
    m = kb.get_full(c, "coding/python/asyncio")["sections"][2]
    assert m["changed_by"] == "external" and m["indexed"] is False


@aio
async def test_external_new_delete_rename(kb):
    c = Ctx()
    d = os.path.join(kb.disk, "coding", "python")
    nb = nbformat.v4.new_notebook()
    nb.cells.append(nbformat.v4.new_markdown_cell("# Hand made\n\nby hand"))
    nb.cells.append(nbformat.v4.new_code_cell("print(1)"))
    nbformat.write(nb, os.path.join(d, "hand.ipynb"))
    os.makedirs(os.path.join(kb.disk, "coding", "newtopic"))
    await kb.refresh()
    r = kb.get(c, "coding/python/hand")
    assert r["head"]["title"] == "Hand made"
    full = kb.get_full(c, "coding/python/hand")
    assert full["sections"][1]["content"] == "```python\nprint(1)\n```"
    assert kb.get(c, "coding/newtopic")["head"]["content"] == "без описания"
    os.replace(os.path.join(d, "hand.ipynb"), os.path.join(d, "hand2.ipynb"))
    await kb.refresh()
    assert (await err(kb.get, c, "coding/python/hand")).code == "not_found"
    assert kb.get(c, "coding/python/hand2")["title"] == "Hand made"


@aio
async def test_admin(kb):
    c = Ctx(profile="admin")
    un = kb.unindexed(c)["items"]
    assert {x["path"] for x in un} >= {"coding", "coding/python", "coding/python/asyncio"}
    full = kb.get_full(c, "coding/python/asyncio")
    v = full["version"]
    secs = [{"id": "head", "title": "Asyncio", "content": "Про asyncio"},
            {"id": "tasks", "title": "Задачи", "content": "x", "tags": ["tasks"]},
            {"id": "event-loop", "title": "Event loop", "content": "y"}]
    r = await kb.put_article(c, "coding/python/asyncio", secs, v)
    assert r["version"] != v
    e = await err(kb.put_article, c, "coding/python/asyncio", secs, v)
    assert e.code == "conflict"
    full = kb.get_full(c, "coding/python/asyncio")
    assert [s["id"] for s in full["sections"]] == ["head", "tasks", "event-loop"]
    assert all(s["indexed"] and s["changed_by"] == "indexer" for s in full["sections"])
    assert full["sections"][1]["tags"] == [{"text": "tasks", "source": "indexer"}]
    assert "coding/python/asyncio" not in {x["path"] for x in kb.unindexed(c)["items"]}
    r = await kb.put_sections(c, "coding/python/asyncio", [{"id": "new", "title": "New", "content": "z"}], full["version"])
    assert kb.get_full(c, "coding/python/asyncio")["sections"][-1]["id"] == "new"
    # tags-only write: title / content omitted -> text kept
    v = kb.get_full(c, "coding/python/asyncio")["version"]
    await kb.put_sections(c, "coding/python/asyncio", [{"id": "tasks", "tags": ["задачи"]}], v)
    t = kb.get_full(c, "coding/python/asyncio")["sections"][1]
    assert (t["title"], t["content"]) == ("Задачи", "x")
    assert t["tags"] == [{"text": "задачи", "source": "indexer"}]
    v = kb.get_full(c, "coding/python/asyncio")["version"]
    e = await err(kb.put_sections, c, "coding/python/asyncio", [{"id": "nope", "tags": ["a"]}], v)
    assert e.code == "invalid_input"
    secs = [{"id": s} for s in ("head", "tasks", "event-loop", "new")]
    await kb.put_article(c, "coding/python/asyncio", secs, v)
    full = kb.get_full(c, "coding/python/asyncio")
    assert [(s["title"], s["content"]) for s in full["sections"]][:2] == [("Asyncio", "Про asyncio"), ("Задачи", "x")]


def test_content_version_ignores_noise():
    import copy
    from kb_mcp.kb import content_version
    nb = store.new_nb(1, "T", "body")
    nb.cells.append(store.new_cell("## S\ntext", "s"))
    store.kbmeta(nb.cells[1])["tags"] = [{"text": "a", "source": "mcp"}]
    v = content_version(nb)
    noisy = copy.deepcopy(nb)
    noisy["metadata"] = {"kernelspec": {"name": "python3"}, "language_info": {"version": "3.11.2"}}
    noisy["nbformat_minor"] = 9
    for c in noisy.cells:
        c["id"] = "zzz-" + c.get("id", "")
        c["metadata"]["collapsed"] = True
        c["metadata"]["kb"].update(updated="2099-01-01T00:00:00Z", changed_by="web", indexed=True, hash="f" * 16)
    assert content_version(noisy) == v
    for change in (lambda n: n.cells[1].__setitem__("source", "## S\ntext2"),
                   lambda n: store.kbmeta(n.cells[1]).__setitem__("tags", []),
                   lambda n: store.kbmeta(n.cells[1]).__setitem__("id", "s2"),
                   lambda n: n.cells.reverse()):
        other = copy.deepcopy(nb)
        change(other)
        assert content_version(other) != v


@aio
async def test_render(kb):
    c = Ctx()
    txt = render.text(kb.get(c, "coding/python/asyncio"), c)
    assert txt.startswith("=== coding/python/asyncio#head\ntitle: Asyncio\n---\nАсинхронность")
    assert "Sections:\ncoding/python/asyncio#event-loop — Event loop" in txt
    c = Ctx()
    txt = render.text(kb.get(c, "coding/python/asyncio#event-loop", with_tags=True), c)
    assert "tags: event loop, concurrency\n---\n" in txt
    c = Ctx()
    txt = render.text(kb.search(c, "zzz"), c)
    assert txt.startswith("(no results)\nHint:")
    c = Ctx(lang="ru")
    assert render.text(kb.search(c, "zzz"), c).startswith("(нет результатов)")
    c = Ctx()
    txt = render.text(kb.get(c, "Coding"), c)
    assert txt.endswith("Note: path normalized to coding")
    e = await err(kb.get, Ctx(), "coding/python/asyncio#evnt-loop")
    t = render.error_text(e)
    assert t.startswith("ERROR not_found: Section") and "Options:\n- coding/python/asyncio#event-loop — Event loop" in t
    c = Ctx(profile="admin")
    txt = render.text(kb.get_full(c, "coding/python/asyncio"), c)
    assert txt.startswith("version: ") and "tags: event loop [mcp], concurrency [mcp]" in txt


def test_kernel_metadata_kept():
    """Notebook metadata belongs to JupyterLab: ensure_meta must not touch it (KB storage#format)."""
    nb = store.new_nb(1, "T", "body")
    store.ensure_meta(nb, "web")
    meta = {"kernelspec": {"name": "python3"}, "language_info": {"name": "python"}}
    nb["metadata"] = dict(meta)
    assert store.ensure_meta(nb, "web") is False
    assert nb["metadata"] == meta


@aio
async def test_default_kernel_meta_on_mcp_writes(kb):
    """MCP / indexer: empty kernelspec -> server default; existing one and web saves untouched (KB storage#format)."""
    c = Ctx()
    default = {"kernelspec": {"display_name": "Python 3 (ipykernel)", "language": "python", "name": "python3"},
               "language_info": {"name": "python", "version": "3.x"}}
    kb.default_nb_meta = default
    f = "kb/coding/python/fresh.ipynb"
    await kb.save_article(c, "coding/python/fresh", "Fresh", "new")
    meta = (await kb.cm.get(f))["content"]["metadata"]
    assert meta["kernelspec"] == default["kernelspec"] and meta["language_info"] == default["language_info"]
    # web save with the user's own kernel: kept, and a later MCP write keeps it too
    model = await kb.cm.get(f)
    model["content"]["metadata"] = {"kernelspec": {"name": "other", "display_name": "Other", "language": "x"}}
    await kb.cm.save({"type": "notebook", "format": "json", "content": model["content"]}, f)
    await kb.save_section(c, "coding/python/fresh#s", "S", "text")
    assert (await kb.cm.get(f))["content"]["metadata"] == {
        "kernelspec": {"name": "other", "display_name": "Other", "language": "x"}}
    # web save of an empty notebook: not filled
    model = await kb.cm.get(f)
    model["content"]["metadata"] = {}
    await kb.cm.save({"type": "notebook", "format": "json", "content": model["content"]}, f)
    assert (await kb.cm.get(f))["content"]["metadata"] == {}


def test_kernel_language_info_from_interpreter():
    import sys
    from ipykernel.ipkernel import IPythonKernel
    from kb_mcp.launch import kernel_language_info
    assert kernel_language_info([sys.executable, "-m", "ipykernel_launcher"]) == IPythonKernel.language_info
    assert kernel_language_info(["no-such-python-xyz"]) is None
    assert kernel_language_info([]) is None


@aio
async def test_replace_lines(kb):
    c = Ctx()
    p = "coding/python/asyncio#tasks"
    await kb.save_section(c, p, "Задачи", "a\nx = 1\nb\nx = 1\nc")
    # several matches -> error, nothing written
    e = await err(kb.replace_lines, c, p, "x = 1", "x = 2")
    assert e.code == "ambiguous" and len(e.options) == 2 and "save_section" in e.hint
    assert "line 2" in e.options[0] and "'a'" in e.options[0]
    # more lines make it unique; trailing spaces / trailing newline ignored
    r = await kb.replace_lines(c, p, "b  \nx = 1\n", "b\nx = 2")
    assert r["action"] == "updated" and r["path"] == p
    assert kb.get(c, p)["section"]["content"] == "a\nx = 1\nb\nx = 2\nc"
    # insert (old line kept in new) and delete (empty new)
    await kb.replace_lines(c, p, "c", "c\nd")
    await kb.replace_lines(c, p, ["a", "x = 1"], "")
    assert kb.get(c, p)["section"]["content"] == "b\nx = 2\nc\nd"
    r = await kb.replace_lines(c, p, "d", "d")
    assert r.get("unchanged")
    sec = kb.articles["coding/python/asyncio"].find("tasks")
    assert sec.title == "Задачи" and sec.level == 2 and sec.meta["changed_by"] == "mcp"
    # part of a line / no match
    e = await err(kb.replace_lines, c, p, "x =", "y =")
    assert e.code == "not_found" and e.options == ["x = 2"]
    e = await err(kb.replace_lines, c, p, "x = 3\nzzz", "q")
    assert e.code == "not_found" and "x = 2" in e.options
    e = await err(kb.replace_lines, c, "coding/python/asyncio", "a", "b")
    assert e.code == "invalid_input" and "new-id" not in e.hint and 'get("coding/python/asyncio")' in e.hint
    e = await err(kb.replace_lines, c, p, "", "b")
    assert e.code == "invalid_input"
    # head too
    await kb.replace_lines(c, "coding/python/asyncio#head", "Асинхронность в Python", "Asyncio в Python")
    assert kb.get(c, "coding/python/asyncio")["head"]["content"] == "Asyncio в Python"
    assert not Ctx(profile="lite").has("replace_lines") and Ctx(profile="std").has("replace_lines")
