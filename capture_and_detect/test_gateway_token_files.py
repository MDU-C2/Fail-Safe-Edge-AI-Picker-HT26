"""Portable credential reads use generated test files, never real credentials."""
import os
import subprocess
import sys

import pytest

from device_service import read_gateway_token
from lynx.picker import read_token


def test_dashboard_and_picker_modules_import_when_fcntl_is_unavailable():
    code = '''
import builtins
original = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl is unavailable on Windows')
    return original(name, *args, **kwargs)
builtins.__import__ = without_fcntl
import placing_grid, named_cups, web_app, device_agent, device_service
import device_dashboard, local_device, lynx.picker
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture(params=[read_gateway_token, read_token], ids=["device-service", "native-picker"])
def reader(request):
    return request.param


def test_regular_token_reads_without_unix_open_flags(reader, tmp_path, monkeypatch):
    path = tmp_path / "gateway-token"
    path.write_bytes(b"offline-test-token\r\n")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    assert reader(path) == "offline-test-token"


@pytest.mark.parametrize("content", [b"", b"a" * 4097, b"two words", b"\xff"])
def test_invalid_or_oversized_token_is_rejected(reader, tmp_path, content):
    path = tmp_path / "gateway-token"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        reader(path)


def test_directory_is_rejected_before_open(reader, tmp_path, monkeypatch):
    def no_open(*args, **kwargs):
        raise AssertionError("Non-regular token must not be opened")
    monkeypatch.setattr(os, "open", no_open)
    with pytest.raises(ValueError, match="regular file"):
        reader(tmp_path)


def test_symlink_is_rejected_without_unix_open_flags(reader, tmp_path, monkeypatch):
    target, link = tmp_path / "target", tmp_path / "gateway-token"
    target.write_bytes(b"offline-test-token")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("This account cannot create symbolic links")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    with pytest.raises(ValueError, match="regular file"):
        reader(link)


def test_replaced_token_during_open_is_rejected(reader, tmp_path, monkeypatch):
    path, replacement = tmp_path / "gateway-token", tmp_path / "replacement"
    path.write_bytes(b"offline-test-token")
    replacement.write_bytes(b"different-offline-token")
    original_open = os.open

    def changed_open(name, flags):
        replacement.replace(path)
        return original_open(name, flags)

    monkeypatch.setattr(os, "open", changed_open)
    with pytest.raises(ValueError, match="regular file"):
        reader(path)
