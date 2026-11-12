# SPDX-License-Identifier: GPL-2.0-or-later
"""Drives Buildroot for a project: configure, build."""
import hashlib
import json
import os
import re
import subprocess

from .config import ConfigError, expand
from .repos import RepoError, log, run

CONFIG_LINE = re.compile(r"^(BR2_[A-Za-z0-9_]+)=(.*)$")
UNSET_LINE = re.compile(r"^# (BR2_[A-Za-z0-9_]+) is not set$")


def render(symbol, value):
    """One .config line for a value of the config: section."""
    if value is True or value == "y":
        return f"{symbol}=y"
    if value is False or value == "n":
        return f"# {symbol} is not set"
    if value == "m" or isinstance(value, int):
        return f"{symbol}={value}"
    text = str(value)
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return f"{symbol}={text}"
    return f'{symbol}="' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_config(text):
    """{symbol: value} of a .config or fragment, 'n' for unset symbols."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        m = CONFIG_LINE.match(line)
        if m:
            values[m.group(1)] = m.group(2)
            continue
        m = UNSET_LINE.match(line)
        if m:
            values[m.group(1)] = "n"
    return values


def read_config(path):
    try:
        with open(path) as f:
            return parse_config(f.read())
    except OSError:
        return {}


class Builder:
    def __init__(self, project, repos, work_dir, out_dir=None):
        self.project = project
        self.repos = repos
        build = project.data.get("build") or {}
        root = build.get("dir") or "build"
        if not os.path.isabs(root):
            root = os.path.join(work_dir, root)
        self.out = out_dir or os.path.join(root, project.name)
        self.br = repos["buildroot"].tree
        self.externals = [repos[name].tree for name in project.externals()]

    def env(self):
        env = dict(os.environ)
        for key, value in (self.project.data.get("env") or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        dl_dir = (self.project.data.get("build") or {}).get("dl_dir")
        if dl_dir:
            env["BR2_DL_DIR"] = expand(dl_dir)
        return env

    def make_cmd(self, *targets):
        cmd = ["make", "-C", self.br, f"O={self.out}"]
        cmd.append("BR2_EXTERNAL=" + ":".join(self.externals))
        return cmd + list(targets)

    def make(self, *targets, interactive=True):
        cmd = self.make_cmd(*targets)
        if interactive:
            if subprocess.run(cmd, env=self.env()).returncode:
                raise RepoError(f"make {' '.join(targets)} failed")
            return None
        p = subprocess.run(cmd, env=self.env(), text=True, capture_output=True)
        if p.returncode:
            raise RepoError(f"make {' '.join(targets)} failed:\n{(p.stdout + p.stderr).strip()[-4000:]}")
        return p.stdout

    # --- configuration -------------------------------------------------

    def defconfig(self):
        """('target', name) or ('file', path) or None."""
        entry = self.project.data.get("defconfig")
        if entry is None:
            return None
        if isinstance(entry, dict):
            return ("file", self.project.locate(entry))
        if os.path.isabs(entry):
            return ("file", entry)
        name = entry if entry.endswith("_defconfig") else entry + "_defconfig"
        for tree in [self.br] + self.externals:
            if os.path.exists(os.path.join(tree, "configs", name)):
                return ("target", name)
        raise ConfigError(f"defconfig {name} not found in Buildroot nor in the externals")

    def fragment_files(self):
        return [(ident, self.project.locate(entry) if isinstance(entry, dict) else entry)
                for ident, entry in self.project.fragments()]

    def inline_fragment(self):
        return [render(sym, value) for sym, value in self.project.config().items()]

    def digest(self):
        h = hashlib.sha256()
        h.update(json.dumps([self.br] + self.externals).encode())
        kind = self.defconfig()
        h.update(json.dumps(kind).encode())
        files = [p for _, p in self.fragment_files()] + ([kind[1]] if kind and kind[0] == "file" else [])
        if kind and kind[0] == "target":
            files += [os.path.join(t, "configs", kind[1]) for t in [self.br] + self.externals
                      if os.path.exists(os.path.join(t, "configs", kind[1]))]
        for path in files:
            with open(path, "rb") as f:
                h.update(f.read())
        h.update("\n".join(self.inline_fragment()).encode())
        for repo in self.repos.values():
            h.update(json.dumps(repo.state(), sort_keys=True).encode())
        return h.hexdigest()

    def configure(self, force=False):
        stamp = os.path.join(self.out, ".brkas-config")
        digest = self.digest()
        if not force and os.path.exists(os.path.join(self.out, ".config")):
            try:
                with open(stamp) as f:
                    if f.read().strip() == digest:
                        return False
            except OSError:
                pass
        os.makedirs(self.out, exist_ok=True)
        kind = self.defconfig()
        log(f"configuring {self.out}" + (f" from {os.path.basename(kind[1])}" if kind else ""))
        if kind is None:
            self.make("defconfig", interactive=False)
        elif kind[0] == "target":
            self.make(kind[1], interactive=False)
        else:
            self.make("defconfig", f"BR2_DEFCONFIG={kind[1]}", interactive=False)

        fragments = [p for _, p in self.fragment_files()]
        inline = self.inline_fragment()
        if inline:
            path = os.path.join(self.out, "brkas-config.fragment")
            with open(path, "w") as f:
                f.write("# config: of the brkas project\n" + "\n".join(inline) + "\n")
            fragments.append(path)
        if fragments:
            run(["sh", os.path.join(self.br, "support/kconfig/merge_config.sh"), "-m",
                 "-O", self.out, os.path.join(self.out, ".config"), *fragments], cwd=self.out)
            self.make("olddefconfig", interactive=False)
        self.check(fragments)
        with open(stamp, "w") as f:
            f.write(digest + "\n")
        return True

    def check(self, fragments):
        """Warns about values Kconfig did not keep (missing dependencies)."""
        wanted = {}
        for path in fragments:
            with open(path) as f:
                wanted.update(parse_config(f.read()))
        got = read_config(os.path.join(self.out, ".config"))
        for symbol, value in wanted.items():
            if got.get(symbol, "n") != value:
                log(f"warning: {symbol}={value} asked, the configuration has "
                    f"{symbol}={got.get(symbol, 'n')} (a dependency missing?)")

    # --- commands ------------------------------------------------------

    def build(self, targets):
        self.configure()
        targets = targets or (self.project.data.get("build") or {}).get("targets") or []
        self.make(*targets)
