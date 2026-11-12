# SPDX-License-Identifier: GPL-2.0-or-later
"""brkas: set up and build Buildroot projects described in YAML files."""
import argparse
import os
import sys

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


def prepare(args):
    """The project and its repos, checked out with the includes they hold."""
    work = os.path.abspath(args.work_dir)
    project = config.load(args.project)
    for _ in range(8):
        repos = make_repos(project, work)
        project.repo_dirs = {name: repo.root for name, repo in repos.items()}
        # repos providing patches to others go first
        for repo in sorted(repos.values(), key=lambda r: bool(_patch_deps(r))):
            repo.checkout(project.locate, update=args.update, force=args.force)
        if not project.missing:
            return project, repos
        project = config.load(args.project, project.repo_dirs)
    raise ConfigError("includes keep pointing to new repos: " + ", ".join(sorted(project.missing)))


def builder(args, project, repos):
    return Builder(project, repos, os.path.abspath(args.work_dir), args.output)


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
