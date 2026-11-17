"""Prepare ordinary Wi-Fi phone access without certificates or device changes."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

from dashboard_access import is_private_lan, load_pairing_code


def _phone_ip(value):
    if not is_private_lan(value):
        raise ValueError("Use this Mac's numeric private Wi-Fi IP address, for example 192.168.1.20.")
    return str(ipaddress.ip_address(value))


def _private_directory(directory):
    directory = Path(directory).expanduser().absolute()
    if directory.is_symlink():
        raise ValueError("The phone settings folder must not be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("The phone settings folder must be owned by you with permissions 700.")
    return directory.resolve()


def create_phone_access(directory, ip, *, port=8087):
    """Create/reuse pairing state, preserving existing API settings and keys."""
    ip = _phone_ip(ip)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("Port must be between 1024 and 65535.")
    directory = _private_directory(directory)
    load_pairing_code(directory / "pairing-code")
    host_in_url = f"[{ip}]" if ":" in ip else ip
    manifest = {"origin": f"http://{host_in_url}:{port}", "port": port, "ip": ip,
                "local_origin": f"http://localhost:{port}", "lan_http": True,
                "pairing_code_file": str(directory / "pairing-code"),
                "assistant_settings": str(directory / "assistant-settings.json")}
    # This file contains addresses and paths only. API settings and existing
    # certificate/key files are intentionally neither opened nor overwritten.
    destination = directory / "phone-access.json"
    if destination.is_symlink() or destination.exists() and not destination.is_file():
        raise ValueError("The phone access manifest must be a regular file.")
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".phone-access-", delete=False) as handle:
            temporary_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    try:
        descriptor = os.open(directory / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write("*\n")
    return manifest


# Preserve imports from the earlier setup helper without generating certificates.
create_phone_identity = create_phone_access


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="This Mac's numeric private Wi-Fi IP address")
    parser.add_argument("--port", type=int, default=8087, help="Dashboard port for Mac and phone (default 8087)")
    parser.add_argument("--directory", default=str(Path.home() / ".config" / "cup-dashboard" / "phone"),
                        help="Private settings folder; an existing private folder can be reused")
    args = parser.parse_args(argv)
    try:
        result = create_phone_access(args.directory, args.ip, port=args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Phone access is ready. No certificates or device settings were changed.")
    print(f"Mac dashboard: {result['local_origin']}")
    print(f"Phone dashboard: {result['origin']}")
    print("On the Mac, open Settings to find the pairing code. Enter that code on the phone.")
    print("Enter the OpenAI API key on the Mac; the phone can use the configured assistant.")
    print("Start the offline dashboard with:")
    command = [sys.executable, str(Path(__file__).with_name("web_app.py")), "--demo", "--lan-http",
               "--host", "0.0.0.0", "--port", str(args.port), "--allowed-origin", result["origin"],
               "--pairing-code-file", result["pairing_code_file"], "--assistant-settings", result["assistant_settings"]]
    print(shlex.join(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
