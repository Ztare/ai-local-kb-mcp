"""Emergency versioning: Dulwich autocommits of kb/ (KB storage#git)."""
import os
import threading

GITIGNORE = ".ipynb_checkpoints/\n.~kbtmp-*\n"
AUTHOR = b"kb-mcp <kb-mcp@localhost>"


class AutoCommit:
    def __init__(self, root, delay=180.0, call_later=None, log=None):
        self.root = os.path.abspath(root)
        self.delay = delay
        self.call_later = call_later  # fn(delay, cb) -> handle; None -> threading.Timer
        self.log = log
        self.pending = {}  # path -> set(who)
        self.timer = None
        self.lock = threading.RLock()
        from dulwich.repo import Repo
        if not os.path.isdir(os.path.join(self.root, ".git")):
            Repo.init(self.root)
        gi = os.path.join(self.root, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(GITIGNORE)

    def notify(self, who, path):
        with self.lock:
            self.pending.setdefault(path, set()).add(who)
            if self.timer is None:
                if self.call_later:
                    self.timer = self.call_later(self.delay, self._fire)
                else:
                    self.timer = threading.Timer(self.delay, self._fire)
                    self.timer.daemon = True
                    self.timer.start()

    def _fire(self):
        with self.lock:
            self.timer = None
        self.commit_now()

    def _files(self):
        out = []
        for d, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if f.startswith(".") and f != ".gitignore":
                    continue
                out.append(os.path.relpath(os.path.join(d, f), self.root).replace(os.sep, "/"))
        return out

    def commit_now(self):
        """Stage everything (adds, edits, deletions) and commit if the tree changed."""
        from dulwich.repo import Repo
        with self.lock:
            pending, self.pending = self.pending, {}
            try:
                repo = Repo(self.root)
                try:
                    index = repo.open_index()
                    tracked = [p.decode("utf-8") for p in index.paths()]
                    paths = sorted(set(self._files()) | set(tracked))
                    wt = repo.get_worktree()
                    if paths:
                        wt.stage(paths)
                    index = repo.open_index()
                    tree = index.commit(repo.object_store)
                    try:
                        head_tree = repo[repo.head()].tree
                    except KeyError:
                        head_tree = None
                    if tree == head_tree or (head_tree is None and not paths):
                        return None
                    who = sorted({w for ws in pending.values() for w in ws}) or ["external"]
                    msg = "kb: " + ", ".join(who)
                    if pending:
                        msg += "\n\n" + "\n".join(sorted(pending))
                    return wt.commit(message=msg.encode("utf-8"), author=AUTHOR, committer=AUTHOR)
                finally:
                    repo.close()
            except Exception:
                if self.log:
                    self.log.exception("kb-mcp: autocommit failed")
                return None
