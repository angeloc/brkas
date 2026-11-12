# SPDX-License-Identifier: GPL-2.0-or-later
"""brkas-container: run brkas in a container with Buildroot's host tools.

The image is built from the Dockerfile shipped with brkas, on a stock
Debian: nothing in it needs Buildroot.

  brkas-container --build-image
  brkas-container build project.yml

The work directory (and the download and output directories of the
project, when outside it) are mounted at the same paths inside the
container, so every path of the project stays valid; the container runs
as the calling user, so the files it writes belong to that user.
"""
import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import resources

from . import __version__, config

DEFAULT_IMAGE = f"brkas:{__version__}"


def engine_cmd(name):
    if name:
        return name
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    sys.exit("brkas-container: neither podman nor docker found")


def build_image(engine, image):
    with tempfile.TemporaryDirectory() as ctx:
        pkg = resources.files("brkas")
        shutil.copytree(str(pkg), os.path.join(ctx, "brkas"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy(str(pkg.joinpath("container/Dockerfile")), os.path.join(ctx, "Dockerfile"))
        return subprocess.run([engine, "build", "-t", image, ctx]).returncode


def image_exists(engine, image):
    return subprocess.run([engine, "image", "inspect", image], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def project_arg(brkas_args):
    return next((a for a in brkas_args if a.endswith((".yml", ".yaml")) or ".yml:" in a), None)


def project_settings(brkas_args):
    """The container: section of the project, when there is one."""
    arg = project_arg(brkas_args)
    if not arg:
        return {}
    try:
        return config.load(arg).data.get("container") or {}
    except config.ConfigError:
        return {}


def mounts_for(brkas_args, work, extra=()):
    """Directories to share: the work directory, those of the project
    outside it (project files, download directory, output root), and the
    extra ones (--mount)."""
    dirs = {work}
    wanted = [os.path.abspath(config.expand(d)) for d in extra]
    arg = project_arg(brkas_args)
    if arg:
        for f in arg.split(":"):
            if f:
                dirs.add(os.path.dirname(os.path.abspath(f)))
        try:
            project = config.load(arg)
            build = project.data.get("build") or {}
            for key in ("dl_dir", "dir"):
                value = build.get(key)
                if value:
                    path = config.expand(value)
                    path = path if os.path.isabs(path) else os.path.join(work, path)
                    os.makedirs(path, exist_ok=True)
                    dirs.add(os.path.abspath(path))
        except config.ConfigError:
            pass  # brkas reports it from inside
    for path in wanted:
        if not os.path.isdir(path):
            sys.exit(f"brkas-container: {path}: no such directory to mount")
        dirs.add(path)
    # nested directories are covered by their parent
    out = []
    for d in sorted(dirs):
        if not any(d == o or d.startswith(o + os.sep) for o in out):
            out.append(d)
    return out


def run(engine, image, brkas_args, work, extra, ssh_dir=None, mounts=(), ssh_agent=False):
    cwd = os.getcwd()
    cmd = [engine, "run", "--rm", "--init"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        cmd.append("-it")
    podman = os.path.basename(engine) == "podman"
    if podman:
        cmd += ["--userns=keep-id"]
    else:
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    shared = mounts_for(brkas_args, work, mounts)
    for d in shared:
        cmd += ["-v", f"{d}:{d}:z"]
    if not any(cwd == d or cwd.startswith(d + os.sep) for d in shared):
        cmd += ["-v", f"{cwd}:{cwd}:z"]
        shared.append(cwd)
    if ssh_dir:
        # read-only: brkas.sh copies it to the ~/.ssh of the container
        cmd += ["-v", f"{os.path.abspath(config.expand(ssh_dir))}:/run/brkas/ssh:ro,z"]
    if ssh_agent:
        sock = os.environ.get("SSH_AUTH_SOCK")
        if not sock or not os.path.exists(sock):
            sys.exit("brkas-container: --ssh-agent: no SSH agent running ($SSH_AUTH_SOCK)")
        cmd += ["-v", f"{os.path.realpath(sock)}:/run/ssh-agent.sock",
                "-e", "SSH_AUTH_SOCK=/run/ssh-agent.sock"]
    # ~ is the home of the user, as outside, so that ~ in the project
    # means the same; an empty one, in memory, with the project mounted in
    home = os.path.expanduser("~")
    if any(home == d or home.startswith(d + os.sep) for d in shared):
        pass  # home itself is shared
    elif podman:
        cmd += ["--mount", f"type=tmpfs,destination={home},tmpfs-mode=0700,chown"]
    else:
        cmd += ["--tmpfs", f"{home}:rw,exec,uid={os.getuid()},gid={os.getgid()},mode=0700"]
    cmd += ["-w", cwd, "-e", f"BRKAS_WORK_DIR={work}", "-e", f"HOME={home}",
            "-e", "BRKAS_USER=" + getpass.getuser()]
    for key in ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "TERM"):
        if key in os.environ:
            cmd += ["-e", f"{key}={os.environ[key]}"]
    cmd += extra + [image] + brkas_args
    return subprocess.run(cmd).returncode


def main(argv=None):
    p = argparse.ArgumentParser(prog="brkas-container", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", help="podman or docker (default: the one installed, podman first)")
    p.add_argument("--image", help=f"image (default: $BRKAS_CONTAINER_IMAGE, container.image of the "
                                   f"project, or {DEFAULT_IMAGE})")
    p.add_argument("--build-image", action="store_true", help="build the image from the bundled Dockerfile")
    p.add_argument("--work-dir", default=os.environ.get("BRKAS_WORK_DIR", "."))
    p.add_argument("--ssh-dir", default=os.environ.get("BRKAS_SSH_DIR"), metavar="DIR",
                   help="ssh keys and configuration for the repos, e.g. ~/.ssh, copied to the "
                        "~/.ssh of the container (default: $BRKAS_SSH_DIR); the container can "
                        "read every key in it, so prefer a directory with only the keys needed")
    p.add_argument("--ssh-agent", action="store_true",
                   help="forward the SSH agent ($SSH_AUTH_SOCK) to the container, for the repos")
    p.add_argument("--mount", action="append", default=[], metavar="DIR",
                   help="share DIR with the container, at the same path and read-write, "
                        "e.g. the sources of your software for brkas shell; may be repeated")
    p.add_argument("--engine-option", action="append", default=[], metavar="OPT",
                   help="extra option for '<engine> run', may be repeated")
    p.add_argument("args", nargs=argparse.REMAINDER, help="brkas command and arguments")
    a = p.parse_args(argv)
    settings = project_settings(a.args)
    engine = engine_cmd(a.engine or settings.get("engine"))
    a.image = a.image or os.environ.get("BRKAS_CONTAINER_IMAGE") or settings.get("image") or DEFAULT_IMAGE
    if a.build_image:
        status = build_image(engine, a.image)
        if status or not a.args:
            return status
    if not a.args:
        p.error("a brkas command is needed, e.g. build project.yml")
    if "xconfig" in a.args:
        p.error("xconfig is not available in the container: use menuconfig")
    if a.image == DEFAULT_IMAGE and not image_exists(engine, a.image):
        # not published anywhere: made here on first use (a custom image is
        # pulled by the engine as usual)
        print(f"brkas-container: no {a.image} image yet, building it (only once)", file=sys.stderr)
        status = build_image(engine, a.image)
        if status:
            return status
    return run(engine, a.image, a.args, os.path.abspath(a.work_dir),
               list(settings.get("options") or []) + a.engine_option, a.ssh_dir, a.mount,
               a.ssh_agent)


if __name__ == "__main__":
    sys.exit(main())
