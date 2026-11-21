"""Answers: flat text for `content` (what the model reads) + JSON for `structuredContent` (KB answers, api#errors)."""


def _block(sec, with_meta=False):
    lines = ["=== " + sec["path"], "title: " + (sec.get("title") or "")]
    if with_meta:
        lines += ["indexed: " + ("true" if sec.get("indexed") else "false"),
                  "updated: " + (sec.get("updated") or ""),
                  "changed_by: " + (sec.get("changed_by") or ""),
                  "hash: " + (sec.get("hash") or "")]
        tags = sec.get("tags") or []
        if tags:
            lines.append("tags: " + ", ".join("%s [%s]" % (tg["text"], tg.get("source", "")) for tg in tags))
    elif "tags" in sec:
        lines.append("tags: " + ", ".join(sec["tags"]))
    lines.append("---")
    body = sec.get("content") or ""
    return "\n".join(lines) + ("\n" + body if body else "")


def _list(items, ctx, label=None, empty=None):
    out = [label] if label else []
    if items:
        out += [it["path"] + " — " + (it.get("title") or "") for it in items]
    else:
        out.append(empty or ctx.t("none"))
    return "\n".join(out)


def text(res, ctx):
    k = res["kind"]
    if k == "root":
        if not res["topics"]:
            return ctx.t("empty_kb")
        out = _list(res["topics"], ctx)
    elif k == "list":
        if not res["items"]:
            out = ctx.t("no_results") + ("\nHint: " + res["empty_hint"] if res.get("empty_hint") else "")
        else:
            out = _list(res["items"], ctx)
    elif k == "tags":
        if not res["tags"]:
            out = ctx.t("no_results") + ("\nHint: " + res["empty_hint"] if res.get("empty_hint") else "")
        else:
            out = "\n".join(res["tags"])
    elif k == "section":
        out = _block(res["section"])
    elif k == "sections":
        out = "\n\n".join(_block(s) for s in res["sections"]) or ctx.t("no_results")
    elif k == "article":
        parts = [_block(res["head"])] if res["head"] else ["=== " + res["path"] + "\ntitle: " + res["title"]]
        if res["full"]:
            parts += [_block(s) for s in res["sections"]]
        else:
            parts.append(_list(res["sections"], ctx, "Sections:"))
        out = "\n\n".join(parts)
    elif k == "topic":
        parts = [_block(res["head"])] if res["head"] else ["=== " + res["path"] + "\ntitle: " + res["title"]]
        parts.append(_list(res["topics"], ctx, "Topics:"))
        parts.append(_list(res["articles"], ctx, "Articles:"))
        out = "\n\n".join(parts)
    elif k == "write":
        action = "unchanged" if res.get("unchanged") else res["action"]
        out = "OK %s %s" % (action, res["path"])
        if res.get("version"):
            out += "\nversion: " + res["version"]
    elif k == "full":
        out = "version: " + (res["version"] or "") + "\n\n" + \
            "\n\n".join(_block(s, with_meta=True) for s in res["sections"])
    elif k == "unindexed":
        if not res["items"]:
            out = ctx.t("no_results")
        else:
            rows = []
            for it in res["items"]:
                row = "%s — %d unindexed" % (it["path"], it["unindexed"])
                if it["oldest"]:
                    row += " — oldest " + it["oldest"]
                if it.get("no_head"):
                    row += " — no head"
                rows.append(row)
            out = "\n".join(rows)
    else:  # pragma: no cover
        out = str(res)
    if ctx.normalized is not None:
        out += "\nNote: " + ctx.t("normalized", path=ctx.normalized or '""')
    return out


def structured(res, ctx):
    body = {k: v for k, v in res.items() if k not in ("kind", "empty_hint")}
    out = {"ok": True, "result": body}
    if ctx.normalized is not None:
        out["normalized"] = ctx.normalized
    return out


def error_text(err):
    out = ["ERROR %s: %s" % (err.code, err.message)]
    if err.hint:
        out.append("Hint: " + err.hint)
    if err.options:
        out.append("Options:")
        for o in err.options:
            if isinstance(o, dict):
                out.append("- " + o["path"] + (" — " + o["title"] if o.get("title") else ""))
            else:
                out.append("- " + str(o))
    return "\n".join(out)


def error_structured(err):
    return {"ok": False, "error": {"code": err.code, "message": err.message,
                                   "hint": err.hint, "options": err.options}}
