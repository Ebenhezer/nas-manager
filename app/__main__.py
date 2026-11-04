"""Run the web application: python -m app"""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings, tighten_umask
from app.main import create_app


def main() -> None:
    tighten_umask()
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # use the application's logging configuration
        access_log=False,  # request logging is done by RequestContextMiddleware
        # Client addresses (throttles, quotas, audit) come from
        # X-Forwarded-For only when the peer is a trusted proxy; an empty
        # NAS_FORWARDED_ALLOW_IPS ignores the headers altogether (uvicorn
        # would otherwise fall back to its own default).
        proxy_headers=bool(settings.forwarded_allow_ips),
        forwarded_allow_ips=settings.forwarded_allow_ips or None,
        server_header=False,
    )


if __name__ == "__main__":
    main()
