"""Launch-time duties moved out of the start scripts (KB setup#token): fixed token, printing addresses."""
import json
import os
import secrets
import shutil
import subprocess

TOKEN_FILE = "token.txt"


def token_file():
    from jupyter_core.paths import jupyter_config_dir
    return os.path.join(jupyter_config_dir(), TOKEN_FILE)


def setup_token(serverapp, log):
    """Explicit token (JUPYTER_TOKEN, --IdentityProvider.token, config) wins; otherwise the fixed
    token from <JUPYTER_CONFIG_DIR>/token.txt, generated there on first start."""
    idp = getattr(serverapp, "identity_provider", None)
    if idp is None or not getattr(idp, "token_generated", False):
        return None                      # set explicitly (or no token auth) - leave as is
    path = token_file()
    tok = ""
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        pass
    if not tok:
        tok = secrets.token_hex(16)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        log.info("kb-mcp: generated token into %s", path)
    idp.token = tok
    idp.token_generated = False
    return path


def default_kernel_meta(serverapp, log=None):
    """kernelspec (+ language_info for Python) of the server's default kernel - what the kernel itself
    writes into a notebook on open, so JupyterLab does not mark an untouched article dirty (KB storage#format)."""
    try:
        ksm = serverapp.kernel_spec_manager
        name = getattr(serverapp.kernel_manager, "default_kernel_name", None) or "python3"
        spec = ksm.get_kernel_spec(name)
    except Exception as e:  # no kernels installed - nothing to fill
        if log:
            log.warning("kb-mcp: no default kernel spec (%s); notebooks keep empty metadata", e)
        return None
    meta = {"kernelspec": {"display_name": spec.display_name, "language": spec.language, "name": name}}
    if (spec.language or "").lower() == "python":
        info = kernel_language_info(spec.argv, log)
        if info:
            meta["language_info"] = info
    return meta


LANG_INFO_CODE = ("import json; from ipykernel.ipkernel import IPythonKernel as K; "
                  "print(json.dumps(K.language_info))")


def kernel_language_info(argv, log=None, timeout=60):
    """Ask the kernel's own interpreter (kernelspec argv[0]) for ipykernel's language_info - exactly what
    the kernel writes into a notebook. None if it cannot be asked (no ipykernel, odd argv, timeout)."""
    exe = (argv or [""])[0]
    if not exe or "{" in exe:
        return None
    exe = shutil.which(exe) or exe
    try:
        out = subprocess.run([exe, "-c", LANG_INFO_CODE], capture_output=True, text=True,
                             timeout=timeout, env=os.environ.copy(), stdin=subprocess.DEVNULL)
        if out.returncode == 0:
            info = json.loads(out.stdout.strip().splitlines()[-1])
            if isinstance(info, dict) and info.get("name"):
                return info
        err = (out.stderr or "").strip().splitlines()[-1:] or ["exit %d" % out.returncode]
        reason = err[0]
    except Exception as e:  # not found, timeout, bad output
        reason = str(e) or e.__class__.__name__
    if log:
        log.warning("kb-mcp: no language_info from kernel %s (%s); writing kernelspec only", exe, reason)
    return None


def print_settings(serverapp, token_path):
    idp = getattr(serverapp, "identity_provider", None)
    tok = getattr(idp, "token", "") or ""
    ip = serverapp.ip if serverapp.ip not in ("", "0.0.0.0", "::", "*") else "127.0.0.1"
    proto = "https" if getattr(serverapp, "certfile", None) else "http"
    base = "%s://%s:%s%s" % (proto, ip, serverapp.port, serverapp.base_url)
    q = "token=" + tok if tok else ""
    src = "  (%s)" % token_path if token_path else ""
    lines = [
        "[kb] token: %s%s" % (tok or "(none)", src),
        "[kb] web:   %slab%s" % (base, "?" + q if q else ""),
        "[kb] mcp:   %smcp/kb?profile=lite%s" % (base, "&" + q if q else ""),
        "[kb] admin: %smcp/kb-admin%s" % (base, "?" + q if q else ""),
    ]
    lines += claude_commands(base, tok)
    print("\n".join(lines), flush=True)


def claude_commands(base, tok):
    """Ready-to-copy `claude mcp add` lines, one per API variant (KB setup#token)."""
    tail = "&lang=ru&off=structured" + ("&token=" + tok if tok else "")
    add = '  claude mcp add --transport http -s local %s "%s"'
    out = ["[kb] Claude Code (run in the project folder; pick one kb profile, kb-admin is for the indexer):"]
    for profile in ("full", "std", "lite"):
        out.append(add % ("kb", "%smcp/kb?profile=%s%s" % (base, profile, tail)))
    out.append(add % ("kb-admin", "%smcp/kb-admin?%s" % (base, tail.lstrip("&"))))
    return out
