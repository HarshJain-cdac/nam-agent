"""The `nam-agent` command (and `python -m nam_agent`):

  nam-agent serve [--host H] [--port P]   the NAM harness server, http://127.0.0.1:7777
                                          (the chat page, /api/* and /v1/nam/*)

`nam-agent dashboard` and `nam-agent nam-api` are older names for serve.
docs/backend-integration.md describes every endpoint.
"""

from __future__ import annotations

import sys


def _tolerant_stdio() -> None:
    """A console that cannot encode a character prints a replacement, not a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _tolerant_stdio()
    args = sys.argv[1:]
    if not args or args[0] in ("serve", "dashboard", "nam-api"):
        from nam_agent.server.http import main as serve

        serve(args[1:])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
