# SPDX-License-Identifier: GPL-2.0-or-later
"""Project files: YAML files with includes, merged kas-style.

A project is one or more YAML files given on the command line, joined
with ':' (later files override earlier ones). Each file may include
others in header.includes, either relative to itself or from a repo
(after that repo is checked out). Includes come first, the including
file overrides them.

Merging: dictionaries merge key by key, recursively; any other value
replaces the previous one; a key set to null is removed. Paths in
fragments, patches and local repos are made absolute (or bound to their
repo) as each file is read, so that they stay relative to the file
that declares them.
"""
import copy
import json
import os
from importlib import resources

import yaml

FORMAT_VERSION = 1


class ConfigError(Exception):
    pass


def merge(base, top):
    """Merges top into base, kas-style; returns a new dictionary."""
    out = copy.deepcopy(base)
    for key, value in top.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _schema():
    return json.loads(resources.files("brkas").joinpath("schema.json").read_text())


def validate(data, where):
    try:
        import jsonschema
    except ImportError:
        return
    try:
        jsonschema.validate(data, _schema())
    except jsonschema.ValidationError as e:
        path = ".".join(str(p) for p in e.absolute_path) or "(top)"
        hint = ""
        if e.validator == "type" and isinstance(e.instance, (int, float)) and not isinstance(e.instance, bool):
            hint = " (YAML reads it as a number: quote it)"
        raise ConfigError(f"{where}: {path}: {e.message}{hint}") from None


def expand(path):
    return os.path.expanduser(path)


def _abs(base_dir, path):
    return os.path.normpath(os.path.join(base_dir, expand(path)))


def _locate(entry, base_dir, repo_dirs):
    """A file reference: a path relative to the declaring file, or
    {repo: name, file: path} inside a repo. Returns an absolute path, or
    None when the repo is not checked out yet."""
    if isinstance(entry, str):
        return _abs(base_dir, entry)
    repo, name = entry.get("repo"), entry.get("file")
    if repo is None:
        return _abs(base_dir, name)
    if repo not in repo_dirs:
        return None
    return os.path.join(repo_dirs[repo], name)


def _bind_paths(data, base_dir):
    """Makes the paths declared in one file independent from it."""
    data = copy.deepcopy(data)

    def bind(entry):
        if isinstance(entry, str):
            return _abs(base_dir, entry)
        if isinstance(entry, dict) and entry.get("repo") is None and "path" in entry:
            entry = dict(entry, path=_abs(base_dir, entry["path"]))
        return entry

    for ident, frag in (data.get("fragments") or {}).items():
        if frag is not None:
            data["fragments"][ident] = bind(frag)
    repos = [data.get("buildroot")] + list((data.get("externals") or {}).values())
    for repo in repos:
        if not repo:
            continue
        if repo.get("dir"):
            repo["dir"] = _abs(base_dir, repo["dir"])
        for ident, patch in (repo.get("patches") or {}).items():
            if patch is not None:
                repo["patches"][ident] = bind(patch)
    defconfig = data.get("defconfig")
    if isinstance(defconfig, str) and "/" in defconfig:
        data["defconfig"] = _abs(base_dir, defconfig)
    elif isinstance(defconfig, dict) and defconfig.get("repo") is None:
        data["defconfig"] = _abs(base_dir, defconfig["file"])
    return data


def load_file(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a YAML mapping")
    version = (data.get("header") or {}).get("version")
    if version != FORMAT_VERSION:
        raise ConfigError(f"{path}: header.version must be {FORMAT_VERSION}, not {version}")
    validate(data, path)
    return data


class Project:
    """The merged configuration of a chain of project files."""

    def __init__(self, files, repo_dirs=None):
        self.files = [os.path.abspath(f) for f in files]
        self.repo_dirs = dict(repo_dirs or {})
        self.missing = set()  # repos whose includes are still to read
        self.sources = []
        self.data = {}
        for path in self.files:
            self.data = merge(self.data, self._load(path, []))
        self.data.pop("header", None)

    def _load(self, path, stack):
        if path in stack:
            raise ConfigError("include loop: " + " -> ".join(stack + [path]))
        data = load_file(path)
        self.sources.append(path)
        base_dir = os.path.dirname(path)
        merged = {}
        for entry in (data.get("header") or {}).get("includes") or []:
            inc = _locate(entry, base_dir, self.repo_dirs)
            if inc is None:
                self.missing.add(entry["repo"])
                continue
            merged = merge(merged, self._load(inc, stack + [path]))
        return merge(merged, _bind_paths(data, base_dir))

    @property
    def name(self):
        return (self.data.get("build") or {}).get("name") or \
            os.path.splitext(os.path.basename(self.files[-1]))[0]

    def externals(self):
        return {k: v for k, v in (self.data.get("externals") or {}).items() if v is not None}

    def buildroot(self):
        br = self.data.get("buildroot")
        if not br:
            raise ConfigError("no buildroot: in the project")
        return br

    def fragments(self):
        """(id, path or {repo, file}) in declaration order."""
        return [(k, v) for k, v in (self.data.get("fragments") or {}).items() if v is not None]

    def config(self):
        return {k: v for k, v in (self.data.get("config") or {}).items() if v is not None}

    def locate(self, entry):
        """An absolute path for a file reference, once repos are checked out."""
        path = _locate(entry, "/", self.repo_dirs)
        if path is None:
            raise ConfigError(f"repo {entry['repo']} is not checked out")
        return path


def load(spec, repo_dirs=None):
    """Loads 'a.yml:b.yml'."""
    files = [f for f in spec.split(":") if f]
    if not files:
        raise ConfigError("no project file given")
    return Project(files, repo_dirs)
