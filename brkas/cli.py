# SPDX-License-Identifier: GPL-2.0-or-later
"""brkas: set up and build Buildroot projects described in YAML files."""
import argparse
import os
import shutil
import subprocess
import sys

import yaml

from . import __version__, config, template
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


def builder(args, project, repos, shell=False):
    return Builder(project, repos, os.path.abspath(args.work_dir), args.output, shell)


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


def cmd_shell(args):
    project, repos = prepare(args)
    return builder(args, project, repos, shell=True).shell(args.command)


def cmd_menuconfig(args):
    project, repos = prepare(args)
    builder(args, project, repos).menuconfig(args.tool, args.save)


CONFIG_TOOLS = {
    "menuconfig": "change the configuration in menuconfig, save the changes as a fragment",
    "xconfig": "the same, in xconfig (Qt)",
}


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


def remove(path):
    if os.path.isdir(path):
        log(f"removing {path}")
        shutil.rmtree(path)


def download_cache(project, work):
    """Where Buildroot downloads for this project: build.dl_dir, else
    $BR2_DL_DIR, else dl/ in the Buildroot tree."""
    dl_dir = (project.data.get("build") or {}).get("dl_dir") or os.environ.get("BR2_DL_DIR")
    if dl_dir:
        return os.path.abspath(config.expand(dl_dir))
    return os.path.join(make_repos(project, work)["buildroot"].tree, "dl")


def remove_cache(path):
    """Empties the cache; the directory itself may stay, as when it is a
    mount point in brkas-container."""
    if not os.path.isdir(path):
        return
    log(f"removing {path}")
    for entry in os.listdir(path):
        entry = os.path.join(path, entry)
        if os.path.isdir(entry) and not os.path.islink(entry):
            shutil.rmtree(entry)
        else:
            os.unlink(entry)
    try:
        os.rmdir(path)
    except OSError:
        pass


def clean_work_dir(work):
    """clean --all without a project: what brkas made in the work
    directory, i.e. build/, downloads/ and the repos it checked out (those
    with its state file)."""
    for name in ("build", "downloads"):
        remove(os.path.join(work, name))
    for entry in sorted(os.listdir(work)) if os.path.isdir(work) else []:
        path = os.path.join(work, entry)
        if os.path.exists(os.path.join(path, ".git", "brkas.json")) or \
                os.path.exists(os.path.join(path, ".brkas.json")):
            remove(path)


def cmd_clean(args):
    work = os.path.abspath(args.work_dir)
    if not args.project:
        if args.cache:
            raise ConfigError("clean --cache needs a project: the cache is its build.dl_dir")
        if not args.all or args.output:
            raise ConfigError("clean needs a project, or --all to clean the whole work directory")
        return clean_work_dir(work)
    project = config.load(args.project)
    if args.cache:
        remove_cache(download_cache(project, work))
        if not args.all:
            return  # only the cache: the output stays
    build = project.data.get("build") or {}
    root = build.get("dir") or "build"
    out_dir = args.output or os.path.join(root if os.path.isabs(root) else os.path.join(work, root), project.name)
    remove(out_dir)
    if args.all:
        for name, repo in make_repos(project, work).items():
            if repo.kind != "local":
                remove(repo.root)


def cmd_init(args):
    """Writes a demo project in a new or empty directory, under git."""
    target = os.path.abspath(args.directory)
    name = os.path.basename(target)
    files = {
        "demo.yml": template.PROJECT,
        "README.md": template.README.format(name=name),
        ".gitignore": template.GITIGNORE,
    }
    existing = [f for f in files if os.path.exists(os.path.join(target, f))]
    if existing:
        raise ConfigError(f"{target}: {', '.join(existing)} already there, brkas init does not "
                          "overwrite files")
    os.makedirs(target, exist_ok=True)
    for path, text in files.items():
        with open(os.path.join(target, path), "w") as f:
            f.write(text)
        log(f"wrote {os.path.join(target, path)}")
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=target,
                            capture_output=True, text=True) if shutil.which("git") else None
    if inside is None:
        log("git not found: the project is not under git")
    elif inside.returncode == 0:
        log(f"{target} is already in a git repository")
    else:
        subprocess.run(["git", "init", "-q", "-b", "main", target], check=True)
        log(f"git repository created in {target}")
    log(f"next: cd {args.directory} && brkas build demo.yml")


# --- arguments ---------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="brkas", description=__doc__)
    p.add_argument("--version", action="version", version=f"brkas {__version__}")
    p.add_argument("--work-dir", default=os.environ.get("BRKAS_WORK_DIR", "."),
                   help="where repos and build/ go (default: $BRKAS_WORK_DIR or .)")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, func, help, checkout=True, project="project file(s), a.yml or a.yml:b.yml"):
        s = sub.add_parser(name, help=help, description=help)
        if project:
            s.add_argument("project", help=project)
        s.set_defaults(func=func)
        if checkout:
            s.add_argument("--update", action="store_true", help="fetch the repos, to follow their branches")
            s.add_argument("--force", action="store_true", help="discard local changes in the repos")
            s.add_argument("-o", "--output", help="output directory (default: build/<name>)")
        return s

    s = sub.add_parser("init", help="create a demo project, with a README and a .gitignore, under git",
                       description="create a demo project, with a README and a .gitignore, under git")
    s.set_defaults(func=cmd_init)
    s.add_argument("directory", nargs="?", default=".", help="where to create it (default: .)")
    s = add("checkout", cmd_checkout, "check out the repos and configure the output directory")
    s.add_argument("--no-config", action="store_true", help="only check out the repos")
    s.add_argument("--reconfigure", action="store_true", help="configure again from scratch")
    s = add("build", cmd_build, "check out, configure and build")
    s.add_argument("--reconfigure", action="store_true", help="configure again from scratch")
    s.add_argument("targets", nargs="*", help="make targets (default: build.targets, else all)")
    s = add("shell", cmd_shell, "bash in the output directory with the SDK environment, after make sdk if needed")
    s.add_argument("-c", "--command", help="run this command instead of an interactive shell")
    for tool, help in CONFIG_TOOLS.items():
        s = add(tool, cmd_menuconfig, help)
        s.set_defaults(tool=tool)
        s.add_argument("--save", metavar="FRAGMENT", help="fragment file to write the changes to (default: print them)")
    s = add("dump", cmd_dump, "print the merged project, or with --lock its exact sources")
    s.add_argument("--lock", action="store_true", help="print the lock file contents")
    s.add_argument("--no-checkout", action="store_true", help="use the repos as they are")
    add("lock", cmd_lock, "pin the repos: write <last project file>.lock.yml")
    s = add("for-all-repos", cmd_for_all_repos, "run a shell command in every repo")
    s.add_argument("--no-checkout", action="store_true", help="use the repos as they are")
    s.add_argument("command", help="command, with BRKAS_REPO_{NAME,DIR,URL,COMMIT} set")
    s = add("clean", cmd_clean, "remove the output directory", checkout=False, project=None)
    s.add_argument("project", nargs="?", help="project file(s); without it, --all cleans the whole "
                                              "work directory")
    s.add_argument("-o", "--output", help="output directory (default: build/<name>)")
    s.add_argument("--all", action="store_true", help="also remove the repos brkas checked out")
    s.add_argument("--cache", action="store_true",
                   help="remove the download cache of the project (build.dl_dir) and nothing else; "
                        "with --all, the cache as well")
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
