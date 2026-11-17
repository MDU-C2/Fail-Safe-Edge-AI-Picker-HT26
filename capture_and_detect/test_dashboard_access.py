"""LAN access checks use an in-process fake app and never contact a gateway."""

import os
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard_access import COOKIE_NAME, LAN_COOKIE_NAME, DashboardAccess, SESSION_SECONDS, load_pairing_code, validate_origin


PHONE_ORIGIN = "https://192.168.1.20:8087"


class DashboardAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = 1000.0
        self.access = DashboardAccess(
            [PHONE_ORIGIN, "http://192.168.1.20:8087", "http://127.0.0.1:8087", "https://localhost:8087"],
            pairing_code_file=self.directory / "pairing-code", clock=lambda: self.now)
        self.app = self.make_app(self.access)

    @staticmethod
    def make_app(access):
        app = FastAPI()

        @app.middleware("http")
        async def guard(request: Request, call_next):
            denied = access.check(request)
            if denied is not None:
                return denied
            if request.method == "POST" and not access.csrf_exempt(request) and request.headers.get("x-csrf-token") != "test-csrf":
                return JSONResponse({"detail": "CSRF required"}, status_code=403)
            return await call_next(request)

        access.install_routes(app)

        @app.get("/")
        @app.get("/api/state")
        def state():
            return {"csrf_token": "test-csrf", "state": "offline"}

        @app.post("/api/command")
        def command():
            return {"accepted": True}

        return app

    def phone(self, *, origin=PHONE_ORIGIN):
        return TestClient(self.app, base_url=origin, client=("192.168.1.50", 41000), follow_redirects=False)

    def pair(self, client, code=None):
        return client.post("/pair", data={"code": code or self.access.pairing_code}, headers={"Origin": PHONE_ORIGIN})

    def test_loopback_is_available_but_cannot_be_spoofed_by_a_remote_client(self):
        local = TestClient(self.app, base_url="http://127.0.0.1:8087", client=("127.0.0.1", 41000))
        self.assertEqual(local.get("/api/state").status_code, 200)
        remote = self.phone(origin="https://localhost:8087")
        self.assertEqual(remote.get("/api/state").status_code, 401)

    def test_remote_http_and_unknown_host_are_blocked(self):
        self.assertEqual(self.phone(origin="http://192.168.1.20:8087").get("/api/state").status_code, 403)
        self.assertEqual(self.phone(origin="https://attacker.example:8087").get("/pair").status_code, 403)

    def test_pairing_protects_reads_and_cookie_retains_csrf_protection(self):
        client = self.phone()
        self.assertEqual(client.get("/").headers["location"], "/pair")
        self.assertEqual(client.get("/api/state").status_code, 401)
        paired = self.pair(client)
        self.assertEqual(paired.status_code, 303)
        cookie = paired.headers["set-cookie"].lower()
        for attribute in ("httponly", "samesite=strict", "secure", "path=/", f"max-age={SESSION_SECONDS}"):
            self.assertIn(attribute, cookie)
        self.assertNotIn(self.access.pairing_code, cookie)
        self.assertEqual(client.get("/api/state").status_code, 200)
        self.assertEqual(client.post("/api/command", json={}, headers={"Origin": PHONE_ORIGIN}).status_code, 403)
        self.assertEqual(client.post("/api/command", json={}, headers={"Origin": PHONE_ORIGIN, "X-CSRF-Token": "test-csrf"}).status_code, 200)
        self.now += SESSION_SECONDS + 1
        self.assertEqual(client.get("/api/state").status_code, 401)

    def test_cross_origin_forms_and_requests_are_rejected(self):
        client = self.phone()
        for origin in (None, "https://evil.example", "https://localhost:8087", "http://192.168.1.20:8087"):
            headers = {"Origin": origin} if origin else {}
            response = client.post("/pair", data={"code": self.access.pairing_code}, headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_pairing_request_size_duplicates_and_rate_limit(self):
        client = self.phone()
        headers = {"Origin": PHONE_ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(client.post("/pair", content="code=" + "x" * 513, headers=headers).status_code, 413)
        duplicated = f"code={self.access.pairing_code}&code={self.access.pairing_code}"
        self.assertEqual(client.post("/pair", content=duplicated, headers=headers).status_code, 403)
        for _ in range(3):
            self.assertEqual(self.pair(client, "WRONG-CODE-HERE").status_code, 403)
        self.assertEqual(self.pair(client).status_code, 429)
        self.now += 61
        self.assertEqual(self.pair(client).status_code, 303)

    def test_no_pairing_file_means_no_remote_access(self):
        access = DashboardAccess([PHONE_ORIGIN])
        client = TestClient(self.make_app(access), base_url=PHONE_ORIGIN, client=("192.168.1.50", 41000))
        self.assertEqual(client.get("/api/state").status_code, 403)

    def test_code_is_private_persistent_and_rejects_symlinks_or_public_files(self):
        path = self.directory / "pairing-code"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_pairing_code(path), self.access.pairing_code)
        link = self.directory / "link"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            load_pairing_code(link)
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            load_pairing_code(path)

    def test_private_key_cannot_be_served_as_a_public_certificate(self):
        wrong = self.directory / "wrong.crt"
        wrong.write_text("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\nprivate\n")
        with self.assertRaises(ValueError):
            DashboardAccess([PHONE_ORIGIN], certificate_file=wrong)

    def test_only_exact_origins_are_accepted(self):
        for value in ("https://*.example", "https://example/path", "https://user:pass@example", "https://example:99999", "https://example/", "https://example#fragment", "https://Example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_origin(value)
        self.assertEqual(validate_origin(PHONE_ORIGIN), PHONE_ORIGIN)

    def test_explicit_private_http_pairing_keeps_cookie_and_csrf_protection(self):
        origin = "http://192.168.1.20:8087"
        access = DashboardAccess([origin, "http://localhost:8087"],
                                 pairing_code_file=self.directory / "pairing-code", allow_lan_http=True)
        client = TestClient(self.make_app(access), base_url=origin, client=("192.168.1.50", 41000), follow_redirects=False)
        self.assertEqual(client.get("/api/state").status_code, 401)
        paired = client.post("/pair", data={"code": access.pairing_code}, headers={"Origin": origin})
        self.assertEqual(paired.status_code, 303)
        cookie = paired.headers["set-cookie"].lower()
        self.assertNotIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(client.get("/api/state").status_code, 200)
        self.assertEqual(client.post("/api/command", json={}, headers={"Origin": origin}).status_code, 403)
        self.assertEqual(client.post("/api/command", json={}, headers={"Origin": origin, "X-CSRF-Token": "test-csrf"}).status_code, 200)
        self.assertEqual(client.post("/pair", data={"code": access.pairing_code}, headers={"Origin": "http://untrusted.example"}).status_code, 403)
        page = client.get("/pair").text
        self.assertIn("Settings", page)
        self.assertNotIn("certificate", page.lower())
        self.assertNotIn("microphone", page.lower())

    def test_lan_http_rejects_public_peers_hostnames_and_forwarded_address_claims(self):
        origin = "http://192.168.1.20:8087"
        access = DashboardAccess([origin, "http://localhost:8087"],
                                 pairing_code_file=self.directory / "pairing-code", allow_lan_http=True)
        app = self.make_app(access)
        for peer in ("8.8.8.8", "2001:4860:4860::8888", "unknown"):
            client = TestClient(app, base_url=origin, client=(peer, 41000))
            self.assertEqual(client.get("/pair", headers={"X-Forwarded-For": "192.168.1.50"}).status_code, 403)
        spoofed_local = TestClient(app, base_url="http://localhost:8087", client=("192.168.1.50", 41000))
        self.assertEqual(spoofed_local.get("/api/state").status_code, 403)
        for bad_origin in ("http://example.local:8087", "http://8.8.8.8:8087", "http://0.0.0.0:8087", "http://[::]:8087"):
            with self.subTest(origin=bad_origin), self.assertRaises(ValueError):
                DashboardAccess([bad_origin], allow_lan_http=True)

    def test_private_ipv6_http_can_pair_when_explicitly_enabled(self):
        origin = "http://[fd12:3456::20]:8087"
        access = DashboardAccess([origin], pairing_code_file=self.directory / "pairing-code", allow_lan_http=True)
        client = TestClient(self.make_app(access), base_url=origin, client=("fd12:3456::50", 41000), follow_redirects=False)
        response = client.post("/pair", data={"code": access.pairing_code}, headers={"Origin": origin})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(client.get("/api/state").status_code, 200)

    def test_http_pairing_uses_a_new_cookie_without_reusing_or_overwriting_https_cookie(self):
        secure_origin = "https://192.168.1.20:8443"
        plain_origin = "http://192.168.1.20:8087"
        access = DashboardAccess([secure_origin, plain_origin], pairing_code_file=self.directory / "pairing-code",
                                 allow_lan_http=True)
        client = TestClient(self.make_app(access), base_url=secure_origin, client=("192.168.1.50", 41000), follow_redirects=False)
        secure = client.post("/pair", data={"code": access.pairing_code}, headers={"Origin": secure_origin})
        self.assertEqual(secure.status_code, 303)
        original_token = client.cookies.get(COOKIE_NAME)
        self.assertTrue(original_token)
        self.assertEqual(client.get(plain_origin + "/api/state").status_code, 401)
        # Even an explicitly supplied HTTPS cookie is not an HTTP credential.
        self.assertEqual(client.get(plain_origin + "/api/state", headers={"Cookie": COOKIE_NAME + "=" + original_token}).status_code, 401)
        plain = client.post(plain_origin + "/pair", data={"code": access.pairing_code}, headers={"Origin": plain_origin})
        self.assertEqual(plain.status_code, 303)
        self.assertTrue(plain.headers["set-cookie"].startswith(LAN_COOKIE_NAME + "="))
        self.assertEqual(client.cookies.get(COOKIE_NAME), original_token)
        self.assertTrue(client.cookies.get(LAN_COOKIE_NAME))
        self.assertEqual(client.get(plain_origin + "/api/state").status_code, 200)
        self.assertEqual(client.get(secure_origin + "/api/state").status_code, 200)


if __name__ == "__main__":
    unittest.main()
