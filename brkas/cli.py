# SPDX-License-Identifier: GPL-2.0-or-later
"""brkas: set up and build Buildroot projects described in YAML files."""
import argparse
import os
import subprocess
import sys

import yaml

from . import __version__, config
from .buildroot import Builder
from .config import ConfigError
from .repos import Repo, RepoError, log


def make_repos(project, work_dir):
    repos = {"buildroot": Repo("buildroot", project.buildroot(), work_dir)}
    for name, spec in project.externals().items():
        if name == "buildroot":
            raise ConfigError("externals: 'buildroot' is reserved for Buildroot itself")
        repos[name] = Repo(name, spec, work_dir)
    return repos


def _patch_deps(repo):
    return {e.get("repo") for e in (repo.spec.get("patches") or {}).values()
            if isinstance(e, dict) and e.get("repo")} - {None}


def prepare(args, checkout=True):
    """The project and its repos, checked out with the includes they hold."""
    work = os.path.abspath(args.work_dir)
    # lock --update makes a new lock: the old one must not pin the repos
    locks = not (args.func is cmd_lock and args.update)
    project = config.load(args.project, locks=locks)
    for _ in range(8):
        repos = make_repos(project, work)
        project.repo_dirs = {name: repo.root for name, repo in repos.items()}
        if checkout:
            # repos providing patches to others go first
            for repo in sorted(repos.values(), key=lambda r: bool(_patch_deps(r))):
                repo.checkout(project.locate, update=args.update, force=args.force)
        elif project.missing:
            raise ConfigError("includes from repos not checked out yet: " + ", ".join(sorted(project.missing))
                              + " (run brkas checkout first)")
        if not project.missing:
            return project, repos
        project = config.load(args.project, project.repo_dirs, locks)
        if not checkout:
            continue
    raise ConfigError("includes keep pointing to new repos: " + ", ".join(sorted(project.missing)))


def builder(args, project, repos):
    return Builder(project, repos, os.path.abspath(args.work_dir), args.output)


def lock_data(repos):
    data = {"header": {"version": config.FORMAT_VERSION}}
    pins = {}
    for name, repo in repos.items():
        pin = repo.lock()
        if pin:
            pins[name] = pin
    if "buildroot" in pins:
        data["buildroot"] = pins.pop("buildroot")
    if pins:
        data["externals"] = pins
    return data


# --- commands ----------------------------------------------------------

def cmd_checkout(args):
    project, repos = prepare(args)
    for name, repo in repos.items():
        log(f"{name}: {repo.describe()} -> {repo.tree}")
    if not args.no_config:
        builder(args, project, repos).configure(force=args.reconfigure)


def cmd_build(args):
    project, repos = prepare(args)
    b = builder(args, project, repos)
    b.configure(force=args.reconfigure)
    b.build(args.targets)


def cmd_dump(args):
    project, repos = prepare(args, checkout=not args.no_checkout)
    data = lock_data(repos) if args.lock else dict({"header": {"version": config.FORMAT_VERSION}}, **project.data)
    yaml.safe_dump(data, sys.stdout, sort_keys=False, default_flow_style=False)


def cmd_lock(args):
    project, repos = prepare(args)
    path = config.lock_path(project.files[-1])
    with open(path, "w") as f:
        f.write("# Written by brkas lock: the exact sources of the project\n")
        yaml.safe_dump(lock_data(repos), f, sort_keys=False, default_flow_style=False)
    log(f"wrote {path}")


def cmd_for_all_repos(args):
    project, repos = prepare(args, checkout=not args.no_checkout)
    status = 0
    for name, repo in repos.items():
        env = dict(os.environ, BRKAS_REPO_NAME=name, BRKAS_REPO_DIR=repo.root,
                   BRKAS_REPO_URL=repo.spec.get("url", ""),
                   BRKAS_REPO_COMMIT=repo.state().get("base", ""))
        log(f"{name}: {args.command}")
        status |= subprocess.run(args.command, shell=True, cwd=repo.root, env=env).returncode
    return status


# --- arguments ---------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="brkas", description=__doc__)
    p.add_argument("--version", action="version", version=f"brkas {__version__}")
    p.add_argument("--work-dir", default=os.environ.get("BRKAS_WORK_DIR", "."),
                   help="where repos and build/ go (default: $BRKAS_WORK_DIR or .)")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, func, help):
        s = sub.add_parser(name, help=help, description=help)
        s.add_argument("project", help="project file(s), a.yml or a.yml:b.yml")
        s.set_defaults(func=func)
        s.add_argument("--update", action="store_true", help="fetch the repos, to follow their branches")
        s.add_argument("--force", action="store_true", help="discard local changes in the repos")
        s.add_argument("-o", "--output", help="output directory (default: build/<name>)")
        return s

    s = add("checkout", cmd_checkout, "check out the repos and configure the output directory")
    s.add_argument("--no-config", action="store_true", help="only check out the repos")
    s.add_argument("--reconfigure", action="store_true", help="configure again from scratch")
    s = add("build", cmd_build, "check out, configure and build")
    s.add_argument("--reconfigure", action="store_true", help="configure again from scratch")
    s.add_argument("targets", nargs="*", help="make targets (default: build.targets, else all)")
    s = add("dump", cmd_dump, "print the merged project, or with --lock its exact sources")
    s.add_argument("--lock", action="store_true", help="print the lock file contents")
    s.add_argument("--no-checkout", action="store_true", help="use the repos as they are")
    add("lock", cmd_lock, "pin the repos: write <last project file>.lock.yml")
    s = add("for-all-repos", cmd_for_all_repos, "run a shell command in every repo")
    s.add_argument("--no-checkout", action="store_true", help="use the repos as they are")
    s.add_argument("command", help="command, with BRKAS_REPO_{NAME,DIR,URL,COMMIT} set")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (ConfigError, RepoError) as e:
        print(f"brkas: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
