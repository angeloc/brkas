# SPDX-License-Identifier: GPL-2.0-or-later
"""Drives Buildroot for a project: configure, build, menuconfig, shell."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from .config import ConfigError, expand
from .repos import RepoError, exclude_from_git, log, run

CONFIG_LINE = re.compile(r"^(BR2_[A-Za-z0-9_]+)=(.*)$")
ENVIRONMENT_SETUP = "BR2_PACKAGE_HOST_ENVIRONMENT_SETUP"
SPINNER = "|/-\\"
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


def config_diff(before, after):
    """Fragment lines that turn before into after."""
    lines = []
    for symbol in sorted(set(before) | set(after)):
        old, new = before.get(symbol, "n"), after.get(symbol, "n")
        if old != new:
            lines.append(f"# {symbol} is not set" if new == "n" else f"{symbol}={new}")
    return lines


def symbol_of(line):
    m = CONFIG_LINE.match(line) or UNSET_LINE.match(line)
    return m.group(1) if m else None


def save_fragment(path, lines):
    """Writes lines in a fragment file: a symbol already in it takes its
    new value, other lines are kept."""
    old = []
    if os.path.exists(path):
        with open(path) as f:
            old = [line.rstrip("\n") for line in f]
    names = {symbol_of(line) for line in lines}
    kept = [line for line in old if symbol_of(line) not in names or symbol_of(line) is None]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(kept + lines) + "\n")


class Builder:
    def __init__(self, project, repos, work_dir, out_dir=None, shell=False):
        self.project = project
        self.repos = repos
        build = project.data.get("build") or {}
        root = build.get("dir") or "build"
        if not os.path.isabs(root):
            root = os.path.join(work_dir, root)
        self.out = out_dir or os.path.join(root, project.name)
        # what to keep out of git: the root of the outputs, or the one asked
        self.out_root = out_dir or root
        self.br = repos["buildroot"].tree
        self.externals = [repos[name].tree for name in project.externals()]
        # brkas shell needs host/environment-setup: the shell enables its
        # package, which then stays on so that a build does not drop it
        self.environment_setup_on = shell or \
            read_config(os.path.join(self.out, ".config")).get(ENVIRONMENT_SETUP) == "y"

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

    def jobs(self):
        """-j with every available core, unless the project sets it in
        MAKEFLAGS; Buildroot only runs packages in parallel with
        BR2_PER_PACKAGE_DIRECTORIES, and ignores it otherwise."""
        if "-j" in self.env().get("MAKEFLAGS", ""):
            return []
        return [f"-j{len(os.sched_getaffinity(0))}"]

    def make_quietly(self, target, what):
        """make target with its output in a log file, and a spinner on a
        terminal meanwhile; the end of the log is shown if it fails."""
        path = os.path.join(self.out, f"brkas-{target}.log")
        tty = sys.stderr.isatty()
        log(f"{what}: make {target}, output in {path}")
        start = time.monotonic()
        with open(path, "w") as out:
            p = subprocess.Popen(self.make_cmd(*self.jobs(), target), env=self.env(), stdout=out,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            try:
                frame = 0
                while p.poll() is None:
                    if tty:
                        elapsed = int(time.monotonic() - start)
                        sys.stderr.write(f"\rbrkas: {what} {SPINNER[frame % len(SPINNER)]} "
                                         f"{elapsed // 60:02d}:{elapsed % 60:02d}")
                        sys.stderr.flush()
                        frame += 1
                    time.sleep(0.2)
            except KeyboardInterrupt:
                p.terminate()
                p.wait()
                raise
            finally:
                if tty:
                    sys.stderr.write("\r\033[K")
                    sys.stderr.flush()
        elapsed = int(time.monotonic() - start)
        if p.returncode:
            with open(path, errors="replace") as f:
                tail = f.readlines()[-30:]
            sys.stderr.write("".join(tail))
            raise RepoError(f"make {target} failed, the whole output is in {path}")
        log(f"{what}: done in {elapsed // 60:02d}:{elapsed % 60:02d}")

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
        lines = [ENVIRONMENT_SETUP + "=y"] if self.environment_setup_on else []
        # config: comes last, it may still turn it off
        return lines + [render(sym, value) for sym, value in self.project.config().items()]

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
        exclude_from_git(self.out_root)
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

    def menuconfig(self, tool, save):
        self.configure()
        config = os.path.join(self.out, ".config")
        before = read_config(config)
        self.make(tool)
        lines = config_diff(before, read_config(config))
        if not lines:
            log("no change")
            return lines
        if save:
            save_fragment(save, lines)
            log(f"{len(lines)} changes saved in {save}")
        else:
            print("\n".join(lines))
        # The saved fragment is part of the project: no reconfigure next time
        with open(os.path.join(self.out, ".brkas-config"), "w") as f:
            f.write(self.digest() + "\n")
        return lines

    def sdk_built(self):
        """make sdk ran: it writes host/share/buildroot/sdk-location."""
        return os.path.exists(os.path.join(self.out, "host", "share", "buildroot", "sdk-location"))

    def environment_setup(self, reconfigured=False):
        """host/environment-setup, after 'make sdk' when the SDK is not
        built yet, or the configuration changed since."""
        config = read_config(os.path.join(self.out, ".config"))
        if config.get(ENVIRONMENT_SETUP, "n") != "y":
            log(f"{ENVIRONMENT_SETUP} is off: a shell without the cross-compilation environment")
            return None
        setup = os.path.join(self.out, "host", "environment-setup")
        if reconfigured or not self.sdk_built() or not os.path.exists(setup):
            self.make_quietly("sdk", "building the SDK")
        return setup

    def shell(self, command=None):
        """bash in the output directory, with host/environment-setup sourced
        (cross compiler, CC, CFLAGS, ARCH, CROSS_COMPILE...), building the
        SDK first if needed."""
        reconfigured = self.configure()
        env = self.env()
        env["BRKAS_OUTPUT"] = self.out
        env["BRKAS_BUILDROOT"] = self.br
        setup = self.environment_setup(reconfigured)
        if command:
            # its aliases (cmake, configure) too: they apply from the next line
            source = f'shopt -s expand_aliases\n. "{setup}" >/dev/null\n' if setup else ""
            return subprocess.run(["bash", "-c", source + command], cwd=self.out, env=env).returncode
        with tempfile.NamedTemporaryFile("w", prefix="brkas-rc.", suffix=".sh") as rc:
            rc.write('[ -f ~/.bashrc ] && . ~/.bashrc\n')
            if setup:
                rc.write(f'. "{setup}"\n')
            rc.write('PS1="(brkas) $PS1"\n')
            rc.flush()
            log(f"shell in {self.out}: exit to leave")
            return subprocess.run(["bash", "--rcfile", rc.name, "-i"], cwd=self.out, env=env).returncode
