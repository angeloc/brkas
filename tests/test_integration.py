# SPDX-License-Identifier: GPL-2.0-or-later
"""A real Buildroot (cloned from a local tree) configured through brkas."""
import os
import subprocess
import textwrap

import pytest

from brkas import cli

BUILDROOT = os.environ.get("BRKAS_TEST_BUILDROOT", os.path.expanduser("~/buildroot"))
pytestmark = pytest.mark.skipif(not os.path.isdir(os.path.join(BUILDROOT, ".git")),
                                reason="needs a Buildroot git tree in $BRKAS_TEST_BUILDROOT")


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, text=True, capture_output=True).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(text))


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("brkas")
    ext = tmp / "ext-src"
    write(ext / "external.desc", "name: BRKAS_TEST\ndesc: brkas test external\n")
    write(ext / "Config.in", "")
    write(ext / "external.mk", "")
    git(ext, "init", "-q", "-b", "main")
    git(ext, "add", ".")
    git(ext, "commit", "-q", "-m", "external")
    write(ext / "patched.txt", "patched\n")
    git(ext, "add", ".")
    git(ext, "commit", "-q", "-m", "add patched.txt")
    patch = git(ext, "format-patch", "-1", "-o", str(tmp / "patches"))
    git(ext, "reset", "-q", "--hard", "HEAD~1")

    br_commit = git(BUILDROOT, "rev-parse", "HEAD")
    proj = tmp / "project"
    write(proj / "fragments/net.config", "BR2_PACKAGE_DROPBEAR=y\n")
    write(proj / "base.yml", f"""\
        header: {{version: 1}}
        buildroot:
          url: file://{BUILDROOT}
          refspec: "{br_commit}"
        externals:
          test:
            url: file://{ext}
            refspec: main
            patches:
              p1: {patch}
        defconfig: qemu_x86_64
        """)
    write(proj / "board.yml", """\
        header:
          version: 1
          includes: [base.yml]
        fragments:
          net: fragments/net.config
        config:
          BR2_TARGET_GENERIC_HOSTNAME: brkas-test
          BR2_PACKAGE_BRKAS_DOES_NOT_EXIST: y
        build: {name: qemu}
        """)
    return tmp, proj


def test_checkout_configures(setup, capsys):
    tmp, proj = setup
    work = tmp / "work"
    project = str(proj / "board.yml")
    assert cli.main(["--work-dir", str(work), "checkout", project]) == 0
    out = capsys.readouterr().out
    assert "BR2_PACKAGE_BRKAS_DOES_NOT_EXIST=y asked" in out
    with open(work / "build/qemu/.config") as f:
        cfg = f.read()
    assert "BR2_PACKAGE_DROPBEAR=y" in cfg
    assert 'BR2_TARGET_GENERIC_HOSTNAME="brkas-test"' in cfg
    assert "BR2_x86_64=y" in cfg
    assert (work / "test/patched.txt").read_text() == "patched\n"
    with open(work / "build/qemu/.br2-external.mk") as f:
        assert "BRKAS_TEST" in f.read()

    # nothing changed: no new configuration
    assert cli.main(["--work-dir", str(work), "checkout", project]) == 0
    assert "configuring" not in capsys.readouterr().out


def test_for_all_repos(setup, capfd):
    tmp, proj = setup
    work = tmp / "work"
    project = str(proj / "board.yml")
    assert cli.main(["--work-dir", str(work), "for-all-repos", "--no-checkout", project,
                     'echo "repo=$BRKAS_REPO_NAME $(git rev-parse HEAD)"']) == 0
    out = capfd.readouterr().out
    assert "repo=buildroot " + git(BUILDROOT, "rev-parse", "HEAD") in out
    assert "repo=test " in out


def test_release_tarball(setup, tmp_path):
    import hashlib
    tarball = tmp_path / "buildroot-test.tar.gz"
    subprocess.run(f"git archive --prefix=buildroot-test/ HEAD | gzip -1 > {tarball}", shell=True,
                   cwd=BUILDROOT, check=True)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    project = tmp_path / "rel.yml"
    write(project, f"""\
        header: {{version: 1}}
        buildroot:
          release: "test"
          tarball: {tarball}
          sha256: {digest}
        defconfig: qemu_x86_64
        """)
    work = tmp_path / "work"
    assert cli.main(["--work-dir", str(work), "checkout", str(project)]) == 0
    assert (work / "buildroot/Makefile").exists()
    assert "BR2_x86_64=y" in (work / "build/rel/.config").read_text()
    write(project, f"""\
        header: {{version: 1}}
        buildroot:
          release: "test"
          tarball: {tarball}
          sha256: {"0" * 64}
        """)
    assert cli.main(["--work-dir", str(work), "checkout", str(project)]) == 1
