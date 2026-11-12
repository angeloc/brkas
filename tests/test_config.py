# SPDX-License-Identifier: GPL-2.0-or-later
import os
import textwrap

import pytest

from brkas import config
from brkas.buildroot import parse_config, render


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(text))
    return path


def test_merge_overrides_and_null_removes():
    base = {"a": {"x": 1, "y": 2}, "b": 1, "c": [1]}
    top = {"a": {"y": 3, "z": 4}, "b": None, "c": [2]}
    assert config.merge(base, top) == {"a": {"x": 1, "y": 3, "z": 4}, "c": [2]}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1, "c": [1]}


def test_includes_paths_and_override(tmp_path):
    write(tmp_path / "common/base.yml", """\
        header: {version: 1}
        buildroot: {url: https://example.org/buildroot.git, refspec: master}
        defconfig: qemu_x86_64
        fragments:
          debug: debug.config
        config:
          BR2_TARGET_GENERIC_HOSTNAME: base
          BR2_PACKAGE_HTOP: y
        """)
    top = write(tmp_path / "board.yml", """\
        header:
          version: 1
          includes: [common/base.yml]
        buildroot: {refspec: 2026.08.1}
        fragments:
          board: board.config
        config:
          BR2_TARGET_GENERIC_HOSTNAME: board
          BR2_PACKAGE_HTOP: null
        """)
    p = config.load(str(top))
    assert p.buildroot() == {"url": "https://example.org/buildroot.git", "refspec": "2026.08.1"}
    assert p.fragments() == [("debug", str(tmp_path / "common/debug.config")),
                             ("board", str(tmp_path / "board.config"))]
    assert p.config() == {"BR2_TARGET_GENERIC_HOSTNAME": "board"}
    assert p.name == "board"
    assert p.sources == [str(tmp_path / "board.yml"), str(tmp_path / "common/base.yml")] or \
        set(p.sources) == {str(tmp_path / "board.yml"), str(tmp_path / "common/base.yml")}


def test_colon_chain_and_lock_file(tmp_path):
    a = write(tmp_path / "a.yml", """\
        header: {version: 1}
        buildroot: {url: u, refspec: master}
        externals:
          ext: {url: e, refspec: main}
        """)
    b = write(tmp_path / "b.yml", """\
        header: {version: 1}
        build: {name: chained}
        """)
    write(tmp_path / "b.lock.yml", """\
        header: {version: 1}
        buildroot: {commit: "1111111111111111111111111111111111111111"}
        externals:
          ext: {commit: "2222222222222222222222222222222222222222"}
        """)
    p = config.load(f"{a}:{b}")
    assert p.buildroot()["commit"].startswith("1111")
    assert p.externals()["ext"] == {"url": "e", "refspec": "main",
                                    "commit": "2222222222222222222222222222222222222222"}
    assert p.name == "chained"


def test_repo_includes_are_reported_until_checked_out(tmp_path):
    top = write(tmp_path / "p.yml", """\
        header:
          version: 1
          includes:
            - repo: ext
              file: brkas/base.yml
        buildroot: {url: u}
        """)
    p = config.load(str(top))
    assert p.missing == {"ext"}
    write(tmp_path / "ext/brkas/base.yml", """\
        header: {version: 1}
        config: {BR2_PACKAGE_HTOP: y}
        """)
    p = config.load(str(top), {"ext": str(tmp_path / "ext")})
    assert not p.missing and p.config() == {"BR2_PACKAGE_HTOP": "y"}


def test_schema_and_loop_errors(tmp_path):
    bad = write(tmp_path / "bad.yml", "header: {version: 1}\nconfig: {HOSTNAME: x}\n")
    with pytest.raises(config.ConfigError, match="config"):
        config.load(str(bad))
    num = write(tmp_path / "num.yml", "header: {version: 1}\nbuildroot: {release: 2026.08}\n")
    with pytest.raises(config.ConfigError, match="quote it"):
        config.load(str(num))
    old = write(tmp_path / "old.yml", "header: {version: 2}\n")
    with pytest.raises(config.ConfigError, match="version"):
        config.load(str(old))
    write(tmp_path / "x.yml", "header: {version: 1, includes: [y.yml]}\n")
    write(tmp_path / "y.yml", "header: {version: 1, includes: [x.yml]}\n")
    with pytest.raises(config.ConfigError, match="loop"):
        config.load(str(tmp_path / "x.yml"))


def test_render_parse():
    assert render("BR2_A", "y") == "BR2_A=y"
    assert render("BR2_A", True) == "BR2_A=y"
    assert render("BR2_A", "n") == "# BR2_A is not set"
    assert render("BR2_A", 4) == "BR2_A=4"
    assert render("BR2_A", "host") == 'BR2_A="host"'
    assert render("BR2_A", '"already"') == 'BR2_A="already"'
    assert render("BR2_A", 'a "b"') == 'BR2_A="a \\"b\\""'
    text = 'BR2_A=y\n# BR2_B is not set\nBR2_C="x"\n# comment\n'
    assert parse_config(text) == {"BR2_A": "y", "BR2_B": "n", "BR2_C": '"x"'}
