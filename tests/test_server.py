"""Integration: real Jupyter Server with the kb_mcp extension (MCP + web-save hooks + git)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "testtoken123"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(method, url, body=None, token=True):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "token " + TOKEN)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    shutil.copytree(os.path.join(HERE, "jupyter_data", "mcp_docs"), root / "mcp_docs")
    cfg = tmp_path_factory.mktemp("cfg")
    (cfg / "jupyter_server_config.json").write_text(
        json.dumps({"ServerApp": {"jpserver_extensions": {"kb_mcp": True}}}))
    port = _free_port()
    env = dict(os.environ, JUPYTER_CONFIG_DIR=str(cfg), JUPYTER_TOKEN=TOKEN,
               JUPYTER_DATA_DIR=str(tmp_path_factory.mktemp("jd")),
               JUPYTER_RUNTIME_DIR=str(tmp_path_factory.mktemp("rt")),
               KB_MCP_COMMIT_DELAY="1")
    log = open(root / "server.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "jupyter_server", "--no-browser",
                          "--port", str(port), "--ServerApp.root_dir=" + str(root)],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            if _req("GET", base + "/api/status")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        p.kill()
        raise RuntimeError(open(root / "server.log").read())
    yield base, root
    p.terminate()
    p.wait(10)
    log.close()


class Mcp:
    def __init__(self, base, query=""):
        self.url = base + "/mcp/kb" + query
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        st, r = _req("POST", self.url, {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        assert st == 200, r
        return r

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r


def test_protocol_and_profiles(server):
    base, root = server
    st, _ = _req("POST", base + "/mcp/kb", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=False)
    assert st == 403
    lite = Mcp(base, "?profile=lite")
    init = lite.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "(lite)" not in init["result"]["instructions"]  # content only, no title line
    assert "search(text)" in init["result"]["instructions"]
    st, _ = _req("POST", lite.url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202
    names = [t["name"] for t in lite.rpc("tools/list")["result"]["tools"]]
    assert names == ["get", "search", "save_article", "save_section"]
    tools = {t["name"]: t for t in lite.rpc("tools/list")["result"]["tools"]}
    assert tools["search"]["inputSchema"]["required"] == ["text"]
    assert "outputSchema" not in tools["search"]
    full = Mcp(base, "?profile=full&lang=ru")
    assert len(full.rpc("tools/list")["result"]["tools"]) == 14
    txt, r = lite.call("delete", path="x")
    assert r["isError"] and txt.startswith("ERROR not_allowed") and "full" in txt
    admin = Mcp(base.replace("/mcp/kb", ""))
    admin.url = base + "/mcp/kb-admin"
    assert len(admin.rpc("tools/list")["result"]["tools"]) == 18
    st, body = _req("GET", base + "/mcp/docs/en/lite/search", token=False)
    assert st == 200 and body.startswith("## search")


def test_mcp_flow_and_web_save_hook(server):
    base, root = server
    m = Mcp(base, "?profile=full")
    assert m.call("save_topic", path="coding", title="Код", content="Про код")[0] == "OK created coding"
    m.call("save_topic", path="coding/python", title="Python", content="Python")
    m.call("save_article", path="coding/python/asyncio", title="Asyncio", content="Асинхронность")
    txt, r = m.call("save_section", path="coding/python/asyncio#event-loop", title="Event loop", content="Цикл событий")
    assert txt == "OK created coding/python/asyncio#event-loop"
    assert r["structuredContent"] == {"ok": True, "result": {"path": "coding/python/asyncio#event-loop", "action": "created"}}
    txt, _ = m.call("get", path="coding/python/asyncio")
    assert "Sections:\ncoding/python/asyncio#event-loop — Event loop" in txt

    # --- web save through the Jupyter contents API (what JupyterLab does) ---
    api = base + "/api/contents/kb/coding/python/asyncio.ipynb"
    st, model = _req("GET", api)
    nb = model["content"]
    nb["cells"][1]["source"] = "## Event loop\n\nПравка из веба: uvloop"
    new_cell = {"cell_type": "markdown", "id": "webcell1", "metadata": {}, "source": "## Из браузера\n\nНовая секция"}
    code_cell = {"cell_type": "code", "id": "webcell2", "metadata": {}, "source": "print(42)",
                 "outputs": [], "execution_count": None}
    nb["cells"] += [new_cell, code_cell]
    st, _ = _req("PUT", api, {"type": "notebook", "format": "json", "content": nb})
    assert st == 200

    # index updated by the post-save hook: changed_by=web (a mtime rescan would say external)
    txt, _ = m.call("search", substrings=["uvloop"])
    assert txt == "coding/python/asyncio#event-loop — Event loop"
    a = Mcp(base)
    a.url = base + "/mcp/kb-admin"
    _, r = a.call("get_full", path="coding/python/asyncio")
    secs = r["structuredContent"]["result"]["sections"]
    assert secs[1]["changed_by"] == "web" and secs[1]["indexed"] is False
    assert secs[2]["title"] == "Из браузера" and secs[2]["changed_by"] == "web"
    web_id = secs[2]["id"]
    assert secs[3]["content"] == "```python\nprint(42)\n```"
    txt, _ = m.call("search", substrings=["Новая секция"])
    assert txt == "coding/python/asyncio#%s — Из браузера" % web_id

    # browser saves again with its stale model (no kb metadata on its new cells): ids must stay
    nb["cells"][2]["source"] = "## Из браузера\n\nНовая секция, вторая правка"
    st, _ = _req("PUT", api, {"type": "notebook", "format": "json", "content": nb})
    _, r = a.call("get_full", path="coding/python/asyncio")
    secs2 = r["structuredContent"]["result"]["sections"]
    assert secs2[2]["id"] == web_id
    assert "вторая правка" in m.call("get", path="coding/python/asyncio#" + web_id)[0]

    # web rename / delete (no hooks in Jupyter): picked up by the mtime check
    st, _ = _req("PATCH", api, {"path": "kb/coding/python/aio.ipynb"})
    assert st == 200
    txt, _ = m.call("search", substrings=["uvloop"])
    assert txt == "coding/python/aio#event-loop — Event loop"
    txt, r = m.call("get", path="coding/python/asyncio")
    assert r["isError"] and "- coding/python/aio — Asyncio" in txt

    # autocommit (delay 1s in tests)
    time.sleep(2.5)
    from dulwich.repo import Repo
    repo = Repo(str(root / "kb"))
    msgs = [e.commit.message.decode() for e in repo.get_walker()]
    repo.close()
    assert any("web" in x for x in msgs) and any("mcp" in x for x in msgs)


def test_off_features(server):
    """off=structured,instructions switches those MCP features off per connection (KB api#connection)."""
    base, _ = server
    plain = Mcp(base, "?profile=full&off=structured,%20instructions,bogus")
    init = plain.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert "instructions" not in init
    txt, r = plain.call("get", path="")
    assert "structuredContent" not in r and r["isError"] is False
    txt, r = plain.call("get", path="no-such-area")
    assert r["isError"] and txt.startswith("ERROR not_found") and "structuredContent" not in r

    normal = Mcp(base, "?profile=full")
    assert "instructions" in normal.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert "structuredContent" in normal.call("get", path="")[1]


def test_admin_rules_from_mcp_docs(server):
    """kb-admin takes its instructions and admin tool texts from mcp_docs/<lang>/admin (KB admin-api#rules)."""
    base, _ = server
    admin = Mcp(base, "?lang=ru")
    admin.url = base + "/mcp/kb-admin?lang=ru"
    init = admin.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["instructions"].startswith("Ты — **индексатор**")
    tools = {t["name"]: t["description"] for t in admin.rpc("tools/list")["result"]["tools"]}
    assert "удаляется" in tools["put_article"]          # from ru/admin.ipynb
    assert tools["get"] and tools["get"] != "get"       # consumer texts from ru/full.ipynb
    st, body = _req("GET", base + "/mcp/docs/en/admin/put-article", token=False)
    assert st == 200 and body.startswith("## put_article")


def test_token_file_and_settings(tmp_path):
    """No explicit token: the plugin generates <JUPYTER_CONFIG_DIR>/token.txt, uses it, prints addresses."""
    root, cfg = tmp_path / "data", tmp_path / "cfg"
    root.mkdir()
    cfg.mkdir()
    (cfg / "jupyter_server_config.json").write_text(
        json.dumps({"ServerApp": {"jpserver_extensions": {"kb_mcp": True}}}))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "JUPYTER_TOKEN"}
    env.update(JUPYTER_CONFIG_DIR=str(cfg), JUPYTER_DATA_DIR=str(tmp_path / "jd"),
               JUPYTER_RUNTIME_DIR=str(tmp_path / "rt"), KB_MCP_COMMIT_DELAY="1")
    logf = tmp_path / "server.log"
    log = open(logf, "w")
    p = subprocess.Popen([sys.executable, "-m", "jupyter_server", "--no-browser",
                          "--port", str(port), "--ServerApp.root_dir=" + str(root)],
                         env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        tok = None
        for _ in range(100):
            if (cfg / "token.txt").exists() and "[kb] admin:" in logf.read_text():
                tok = (cfg / "token.txt").read_text().strip()
                break
            time.sleep(0.2)
        assert tok and len(tok) == 32, logf.read_text()
        out = logf.read_text()
        assert ":%d/mcp/kb?profile=lite&token=%s" % (port, tok) in out
        assert "[kb] web:" in out and "[kb] token: " + tok in out
        for name, q in (("kb", "mcp/kb?profile=full&"), ("kb", "mcp/kb?profile=lite&"), ("kb-admin", "mcp/kb-admin?lang=ru")):
            assert 'claude mcp add --transport http -s local %s "' % name in out
            assert ":%d/%s" % (port, q) in out
        assert "off=structured&token=%s\"" % tok in out
        r = urllib.request.Request(base + "/api/status")
        r.add_header("Authorization", "token " + tok)
        with urllib.request.urlopen(r, timeout=20) as resp:
            assert resp.status == 200
        r = urllib.request.Request(base + "/api/status")
        r.add_header("Authorization", "token wrong")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(r, timeout=20)
    finally:
        p.terminate()
        p.wait(10)
        log.close()



def test_default_kernel_meta_live(server):
    """A new article written through MCP carries the server's default kernelspec and its Python language_info."""
    import platform
    base, root = server
    m = Mcp(base, "?profile=full")
    m.call("save_topic", path="kmeta", title="K", content="k")
    m.call("save_topic", path="kmeta/t", title="T", content="t")
    m.call("save_article", path="kmeta/t/a", title="A", content="a")
    nb = json.loads((root / "kb" / "kmeta" / "t" / "a.ipynb").read_text(encoding="utf-8"))
    ks = nb["metadata"].get("kernelspec") or {}
    assert ks.get("name") and ks.get("language") == "python", nb["metadata"]
    assert nb["metadata"]["language_info"]["version"] == platform.python_version()
    from ipykernel.ipkernel import IPythonKernel
    assert nb["metadata"]["language_info"] == IPythonKernel.language_info  # exactly what the kernel writes
