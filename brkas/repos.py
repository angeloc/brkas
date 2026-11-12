# SPDX-License-Identifier: GPL-2.0-or-later
"""The source trees of a project: Buildroot and the br2-externals.

A repo is one of:
  - git: url + refspec (branch, tag or commit) or commit (from a lock
    file), cloned in <work dir>/<name> and checked out detached;
  - local: dir: an existing directory, used as it is.

Patches are applied after the checkout, in id order, then file name
order for a directory: git am for mails, git apply for plain diffs.
A state file records the base and the
patches applied, so that a checkout is redone only when they change,
and a tree with local changes is never touched without --force.
"""
import hashlib
import json
import os
import subprocess

from .config import ConfigError

GIT_ID = ["-c", "user.name=brkas", "-c", "user.email=brkas@localhost"]


class RepoError(Exception):
    pass


def log(msg):
    print(f"brkas: {msg}", flush=True)


def run(cmd, cwd=None, check=True, quiet=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=quiet)
    if check and p.returncode:
        detail = (p.stderr or p.stdout or "").strip() if quiet else ""
        raise RepoError(f"{' '.join(cmd)} failed" + (f":\n{detail}" if detail else ""))
    return p


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Repo:
    def __init__(self, name, spec, work_dir):
        self.name = name
        self.spec = spec
        self.work_dir = work_dir
        if spec.get("dir"):
            self.kind = "local"
            self.root = spec["dir"]
        elif spec.get("url"):
            self.kind = "git"
            self.root = os.path.join(work_dir, name)
        else:
            raise ConfigError(f"{name}: needs url: or dir:")
        if self.kind == "local" and spec.get("patches"):
            raise ConfigError(f"{name}: patches need a repo brkas manages, not dir:")

    @property
    def tree(self):
        """The directory that matters: the external inside the repo."""
        return os.path.normpath(os.path.join(self.root, self.spec.get("path") or "."))

    # --- patches -------------------------------------------------------

    def patch_files(self, locate):
        files = []
        for ident, entry in (self.spec.get("patches") or {}).items():
            if entry is None:
                continue
            path = entry if isinstance(entry, str) else locate({"repo": entry.get("repo"), "file": entry["path"]})
            if os.path.isdir(path):
                files += sorted(os.path.join(path, f) for f in os.listdir(path) if f.endswith((".patch", ".diff")))
            elif os.path.isfile(path):
                files.append(path)
            else:
                raise RepoError(f"{self.name}: patch {ident}: {path} not found")
        return files

    # --- git -----------------------------------------------------------

    def git(self, *args, check=True):
        return run(["git", *GIT_ID, *args], cwd=self.root, check=check)

    def _rev(self, ref):
        p = self.git("rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def resolve(self, update=False):
        """The commit to check out, fetching when needed."""
        commit = self.spec.get("commit")
        refspec = self.spec.get("refspec")
        if update and not commit:
            self.git("fetch", "-q", "--tags", "--force", "origin")
        for attempt in range(2):
            if commit:
                rev = self._rev(commit)
            elif refspec:
                rev = self._rev("origin/" + refspec) or self._rev("refs/tags/" + refspec) or self._rev(refspec)
            else:
                rev = self._rev("origin/HEAD")
            if rev:
                return rev
            if attempt == 0:
                self.git("fetch", "-q", "--tags", "--force", "origin")
                if commit:
                    self.git("fetch", "-q", "origin", commit, check=False)
        raise RepoError(f"{self.name}: {commit or refspec or 'HEAD'} not found in {self.spec['url']}")

    def _state_file(self):
        return os.path.join(self.root, ".git", "brkas.json")

    def state(self):
        try:
            with open(self._state_file()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        with open(self._state_file(), "w") as f:
            json.dump(state, f, indent=1)

    def _checkout_git(self, locate, update, force):
        cloned = False
        if not os.path.isdir(os.path.join(self.root, ".git")):
            if os.path.exists(self.root) and os.listdir(self.root):
                raise RepoError(f"{self.name}: {self.root} exists and is not a git repo")
            log(f"{self.name}: cloning {self.spec['url']}")
            run(["git", "clone", "-q", "--no-checkout", self.spec["url"], self.root])
            cloned = True
            force = True  # an empty work tree after --no-checkout
        base = self.resolve(update)
        patches = self.patch_files(locate)
        wanted = {"base": base, "patches": [sha256(p) for p in patches]}
        state = self.state()
        head = self._rev("HEAD")
        dirty = bool(self.git("status", "--porcelain", "--untracked-files=no").stdout.strip())
        if {k: state.get(k) for k in wanted} == wanted and head == state.get("head") and not dirty:
            return
        if dirty and not force:
            raise RepoError(f"{self.name}: local changes in {self.root}, commit or stash them, or use --force")
        if head and state and head != state.get("head") and not force:
            raise RepoError(f"{self.name}: {self.root} is not at the commit brkas left it at, "
                            "check it out yourself or use --force")
        log(f"{self.name}: checking out {base[:12]}" + (f" + {len(patches)} patches" if patches else ""))
        self.git("checkout", "-q", "--detach", *(["--force"] if force else []), base)
        for patch in patches:
            self._apply_git(patch)
        self._save_state(dict(wanted, head=self._rev("HEAD")))

    def _apply_git(self, patch):
        if self.git("am", "-q", "--keep-cr", patch, check=False).returncode == 0:
            return
        self.git("am", "--abort", check=False)
        if self.git("apply", "--index", patch, check=False).returncode:
            raise RepoError(f"{self.name}: {os.path.basename(patch)} does not apply")
        self.git("commit", "-q", "-m", f"brkas: {os.path.basename(patch)}")

    # --- common --------------------------------------------------------

    def checkout(self, locate, update=False, force=False):
        if self.kind == "git":
            self._checkout_git(locate, update, force)
        elif not os.path.isdir(self.root):
            raise RepoError(f"{self.name}: {self.root} not found")

    def describe(self):
        if self.kind == "git":
            return f"{self.spec['url']} {self.state().get('base', '?')[:12]}"
        return self.root
