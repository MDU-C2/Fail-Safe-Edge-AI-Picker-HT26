"""Phone setup changes only its private files; it needs no network or certificates."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from phone_setup import create_phone_access, main


class PhoneSetupTests(unittest.TestCase):
    def test_only_numeric_private_wifi_addresses_and_nonprivileged_ports_are_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "phone"
            for address in ("127.0.0.1", "0.0.0.0", "::", "::1", "8.8.8.8", "mac.local",
                            "224.0.0.1", "192.168.1.20\nmalicious", "fe80::1%en0"):
                with self.subTest(address=address), self.assertRaises(ValueError):
                    create_phone_access(target, address)
            for port in (True, 80, 1023, 65536, "8087"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    create_phone_access(target, "192.168.1.20", port=port)
            self.assertFalse(target.exists())

    def test_setup_is_private_reuses_pairing_and_preserves_api_settings_and_old_keys(self):
        with tempfile.TemporaryDirectory() as temporary, patch("subprocess.run") as command:
            target = Path(temporary) / "phone"
            result = create_phone_access(target, "192.168.1.20")
            self.assertEqual(result["origin"], "http://192.168.1.20:8087")
            self.assertEqual(result["local_origin"], "http://localhost:8087")
            self.assertTrue(result["lan_http"])
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            for name in ("pairing-code", "phone-access.json"):
                self.assertEqual((target / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual({path.suffix for path in target.iterdir()} & {".crt", ".key", ".pem", ".csr"}, set())
            pairing = (target / "pairing-code").read_bytes()
            private_settings = target / "assistant-settings.json"
            private_settings.write_text('{"test_existing_api_settings": true}')
            old_key = target / "dashboard.key"
            old_key.write_text("old private key fixture must remain unchanged")
            before = {path: path.read_bytes() for path in (private_settings, old_key)}
            changed = create_phone_access(target, "192.168.1.21", port=8088)
            self.assertEqual(changed["origin"], "http://192.168.1.21:8088")
            self.assertEqual((target / "pairing-code").read_bytes(), pairing)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertEqual(json.loads((target / "phone-access.json").read_text()), changed)
            self.assertEqual((target / ".gitignore").read_text(), "*\n")
            command.assert_not_called()

    def test_private_ipv6_address_is_bracketed_in_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = create_phone_access(Path(temporary) / "phone", "fd12:3456::20")
            self.assertEqual(result["origin"], "http://[fd12:3456::20]:8087")

    def test_public_or_symlinked_existing_settings_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "phone"
            folder.mkdir()
            os.chmod(folder, 0o755)
            with self.assertRaises(ValueError):
                create_phone_access(folder, "192.168.1.20")
            os.chmod(folder, 0o700)
            link = Path(temporary) / "link"
            link.symlink_to(folder)
            with self.assertRaises(ValueError):
                create_phone_access(link, "192.168.1.20")

    def test_cli_prints_simple_http_command_without_exposing_pairing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "phone"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--ip", "192.168.1.20", "--directory", str(folder)]), 0)
            text = output.getvalue()
            self.assertIn("--lan-http", text)
            self.assertIn("--host 0.0.0.0", text)
            self.assertIn("--allowed-origin http://192.168.1.20:8087", text)
            self.assertIn("Settings", text)
            self.assertNotIn("--tls", text)
            self.assertNotIn("--phone-ca", text)
            self.assertNotIn((folder / "pairing-code").read_text().strip(), text)


if __name__ == "__main__":
    unittest.main()
