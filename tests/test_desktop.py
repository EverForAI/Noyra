from __future__ import annotations

import unittest

from noyra.desktop import desktop_url


class DesktopLauncherTestCase(unittest.TestCase):
    def test_wildcard_bind_uses_loopback_browser_url(self) -> None:
        self.assertEqual(desktop_url("0.0.0.0", 8765), "http://127.0.0.1:8765/")
        self.assertEqual(desktop_url("localhost", 9000), "http://localhost:9000/")
        self.assertEqual(desktop_url("::1", 9000), "http://[::1]:9000/")
