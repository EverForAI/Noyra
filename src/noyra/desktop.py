from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import threading
import webbrowser

from .service import NoyraService


def desktop_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="noyra-desktop")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    service = NoyraService.from_env()
    if not args.no_browser:
        host, port = service.http.address
        threading.Timer(
            1.0,
            webbrowser.open,
            args=(desktop_url(host, port),),
        ).start()
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
