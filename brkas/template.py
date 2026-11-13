# SPDX-License-Identifier: GPL-2.0-or-later
"""The files that "brkas init" writes in a new project."""

PROJECT = """\
# A brkas project: what to build, and from which sources.
# Build it with: brkas build demo.yml
header:
  version: 1

# Buildroot, from its release tarball. To use git instead:
#   url: https://gitlab.com/buildroot.org/buildroot.git
#   refspec: "2026.08.1"
buildroot:
  release: "2026.08.1"
  sha256: 38a6c152490096018720a9ade379ee1e571c9bf5f93a3657a166566945a98b0d

# br2-external trees, for example:
#   externals:
#     mine:
#       url: https://example.com/my-external.git
#       refspec: main

# Without a defconfig, brkas starts from the default configuration of
# Buildroot. To start from a board instead, for example:
#   defconfig: raspberrypi4_64

# Single options, applied on top. These make a small x86_64 system that
# uses a prebuilt toolchain and has no kernel, so it builds in minutes.
config:
  BR2_x86_64: y
  BR2_TOOLCHAIN_EXTERNAL: y
  BR2_TOOLCHAIN_EXTERNAL_BOOTLIN: y
  BR2_TOOLCHAIN_EXTERNAL_BOOTLIN_X86_64_GLIBC_STABLE: y
  BR2_PACKAGE_DROPBEAR: y
  BR2_TARGET_GENERIC_HOSTNAME: demo
  BR2_PER_PACKAGE_DIRECTORIES: y

build:
  dl_dir: ~/.cache/brkas/dl   # the download cache, shared by all projects
"""

README = """\
# {name}

A Buildroot project, built with [brkas](https://github.com/angeloc/brkas):

```
brkas build demo.yml
```

The result is in `build/demo/images/`.
"""

GITIGNORE = """\
# What brkas downloads and builds in the project directory. brkas also lists
# every directory it creates, externals included, in .git/info/exclude.
/build/
/buildroot/
/downloads/
"""
