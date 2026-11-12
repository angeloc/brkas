#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# brkas in the container. docker runs it as the calling user, whose uid
# has no entry in /etc/passwd: add one, ssh and the prompt need it.
if ! getent passwd "$(id -u)" >/dev/null; then
	echo "${BRKAS_USER:-brkas}:x:$(id -u):$(id -g)::${HOME}:/bin/bash" >> /etc/passwd
fi
if ! getent group "$(id -g)" >/dev/null; then
	echo "${BRKAS_USER:-brkas}:x:$(id -g):" >> /etc/group
fi
# --ssh-dir: a copy, so that ssh can write known_hosts
if [ -d /run/brkas/ssh ]; then
	mkdir -p "$HOME" && rm -rf "$HOME/.ssh" && cp -rL /run/brkas/ssh "$HOME/.ssh" &&
		chmod -R go-rwx "$HOME/.ssh"
fi
exec env PYTHONPATH=/opt/brkas python3 -m brkas "$@"
