"""One place for "what host is this?" — the fork's Windows-maintenance seam.

Upstream scatters `os.name == "nt"` / `sys.platform` checks through the plugin, so a
Windows behavior change means finding them all. The fork asks here instead. Stdlib
only, no sibling imports, so it is safe to import from anywhere (including the
scripts' `import x` fallback path).

Import like the other plugin modules — dual-import idiom:

    if __package__:
        from . import platforms
    else:
        import platforms
"""

import os
import sys


def is_windows() -> bool:
    return os.name == "nt"


def is_mac() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_linux_gateway_host() -> bool:
    """True only on Linux: bot_screen and per-Bot gateway install need a Linux
    gateway host; on macOS and Windows there is no separate screen/host."""
    return not is_windows() and not is_mac()
