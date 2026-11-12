# brkas

brkas does for [Buildroot](https://buildroot.org) what kas does for Yocto.

You describe your project in a YAML file:

- which Buildroot version to use;
- which br2-external trees to add;
- which defconfig to start from;
- which configuration changes to apply on top of it.

Then brkas downloads everything, configures Buildroot and builds the project with a
single command.

Here is a small project file:

```yaml
header:
  version: 1

buildroot:
  url: https://gitlab.com/buildroot.org/buildroot.git
  refspec: 2026.08.1

externals:
  yabi:
    url: https://github.com/angeloc/yabi.git
    refspec: main
    path: br-yabi

defconfig: arietta_xterm01_yabi

config:
  BR2_TARGET_GENERIC_HOSTNAME: demo
```

To build it:

```
brkas build project.yml
```

If you know kas, this table shows what each part of kas becomes in brkas:

| kas (Yocto)                      | brkas (Buildroot)                                   |
|----------------------------------|-----------------------------------------------------|
| `repos:`                         | `buildroot:` and `externals:`                       |
| `machine:`, `distro:`            | `defconfig:`                                        |
| `local_conf_header:`             | `fragments:` (files) and `config:` (single options) |
| `kas build`, `checkout`          | `brkas build`, `checkout`                           |

## Installation

```
pipx install .             # or: pipx install '.[schema]' to also check the project files
```

This installs the `brkas` command.

brkas needs Python 3.10 or newer and PyYAML. It also needs the tools Buildroot itself
needs (make, gcc, git and so on).

## The project file

You can split a project into several files. `brkas build a.yml:b.yml` reads `a.yml` first,
then `b.yml`. Values in `b.yml` replace the ones in `a.yml`.

Here is every section, with comments:

```yaml
header:
  version: 1                 # required, always 1 for now
  includes:                  # other files to read first; this file wins over them
    - common.yml             # a path, relative to this file
    - repo: yabi             # or a file inside a repository
      file: br-yabi/brkas/base.yml

buildroot:                   # where Buildroot comes from, pick one way:
  url: https://gitlab.com/buildroot.org/buildroot.git
  refspec: 2026.08.1         #   a git repository, at a branch, tag or commit;
  # dir: ../buildroot        #   a directory you already have, used as it is
  patches:                   # patches applied after the download
    fix: patches/buildroot   #   a patch file, or a directory of *.patch files
    other:                   #   or patches from a repository
      repo: yabi
      path: patches/buildroot

externals:                   # br2-external trees, same choices as buildroot:
  yabi:
    url: https://github.com/angeloc/yabi.git
    refspec: main
    path: br-yabi            # where the external is inside the repository
  local:
    dir: ../my-external

defconfig: arietta_xterm01_yabi  # a defconfig name from Buildroot or an external,
                                 # or a file path (with a /), or {repo:, file:}

fragments:                   # configuration fragments added to the defconfig, in order
  debug: fragments/debug.config
  board:
    repo: yabi
    file: br-yabi/fragments/x.config

config:                      # single options, applied last
  BR2_TARGET_GENERIC_HOSTNAME: demo   # strings get quotes automatically
  BR2_PACKAGE_DROPBEAR: y             # y, n or m
  BR2_TARGET_ROOTFS_EXT2_SIZE: "128M"

build:
  dir: build                 # where outputs go (default: build/)
  name: demo                 # the output directory name (default: the file name)
  targets: [all]             # what "brkas build" makes by default
  dl_dir: ~/.cache/brkas/dl  # download cache, shared between projects

env:                         # environment variables for make
  MAKEFLAGS: -j12            # with BR2_PER_PACKAGE_DIRECTORIES: y, builds
                             # several packages at the same time
```

Some rules to know:

- When two files set the same section, their contents are merged. Inside a section, a
  later value replaces an earlier one.
- Setting a key to `null` removes it. For example, `fragments: {debug: null}` removes a
  fragment that an included file added.
- A path is relative to the file where it is written.
- Quote values that look like numbers, such as `refspec: "2026.08"`. Otherwise YAML reads
  them as numbers and brkas rejects them.

After configuring, brkas checks the result. If an option you asked for is not in the final
configuration, brkas prints a warning. This usually means the option depends on another
option that is not enabled.

## Commands

```
brkas checkout project.yml          # download everything and configure
brkas build project.yml [targets]   # the same, then build
```

brkas puts the downloaded repositories in the work directory: `buildroot/`, plus one
directory for each external. Builds go in `build/`. The work directory is the current
directory, unless you set `--work-dir` or `$BRKAS_WORK_DIR`.

Two options control the repositories:

- `--update` fetches new commits, so that a branch moves forward.
- `--force` throws away your local changes in the repositories. Without it, brkas stops
  instead of touching them.

## Tests

```
python3 -m pytest tests
```

The integration tests need a Buildroot git tree. They use `$BRKAS_TEST_BUILDROOT`, or
`~/buildroot` by default.

## License

GPL-2.0-or-later. See COPYING.
