#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# brdevtool in the container, from /opt/brdevtool. PYTHONPATH is left as
# it is: the builds that brdevtool starts inherit it.
exec python3 -P -c 'import sys
sys.path.insert(0, "/opt/brdevtool")
sys.argv[0] = "brdevtool"
from brdevtool.cli import main
sys.exit(main())' "$@"
