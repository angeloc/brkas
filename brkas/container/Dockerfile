# SPDX-License-Identifier: GPL-2.0-or-later
# brkas container: a stock Debian with the host packages Buildroot needs
# (docs/manual/prerequisite.adoc: mandatory, plus ncurses for menuconfig,
# git and ssh for the repos, cmake for Buildroot's cmake packages and for
# brkas shell), and brkas. Nothing in it comes from
# Buildroot.
FROM debian:trixie-slim

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash bc binutils build-essential bzip2 ca-certificates cmake cpio \
        debianutils diffutils file findutils g++ gawk gcc git gzip \
        libncurses-dev locales make openssh-client patch perl python3 \
        python3-jsonschema python3-yaml rsync sed tar unzip wget xz-utils && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen && locale-gen

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# brkas.sh adds the calling user to them
RUN chmod 666 /etc/passwd /etc/group

COPY brkas /opt/brkas/brkas
COPY brkas/container/brkas.sh /usr/local/bin/brkas
RUN chmod 755 /usr/local/bin/brkas

ENTRYPOINT ["brkas"]
