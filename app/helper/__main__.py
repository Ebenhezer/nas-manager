"""Run the privileged helper: ``python -m app.helper``.

The helper is a separate root process (see ``deploy/systemd/nas-manager-helper.service``)
that serves the fixed allowlist of operations from ``app/helper/operations.py``
over a Unix socket. The web process connects to it through
``app/services/helper_client.py`` and never gains privileges itself.
"""

from __future__ import annotations

import sys


def main() -> int:
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.helper.server import serve

    settings = get_settings()
    configure_logging(settings)
    serve(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
