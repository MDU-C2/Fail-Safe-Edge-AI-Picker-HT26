"""
pytest plugin: which gateway implementation the suite starts.

    --impl python   the Python gateway (default, or GATEWAY_IMPL)
    --impl cpp      the C++ gateway at --cpp-binary (default
                    GATEWAY_CPP_BINARY or gateway/cpp/build/dev/gateway)

Loaded from pytest.ini (-p gateway.tests.implementation) rather than
conftest.py, so the options are known before the arguments are read:
otherwise "--impl cpp" makes pytest take "cpp" for a test path.
"""

import pytest

from gateway.tests import support


def pytest_addoption(
    parser: pytest.Parser,
) -> None:
    parser.addoption(
        "--impl",
        choices=support.IMPLEMENTATIONS,
        default=support.implementation,
        help="Gateway implementation to start: python (default, or GATEWAY_IMPL) or cpp",
    )

    parser.addoption(
        "--cpp-binary",
        default=support.cpp_binary,
        help=(
            "C++ gateway binary, relative to the repository root "
            "(default: GATEWAY_CPP_BINARY or gateway/cpp/build/dev/gateway)"
        ),
    )


def pytest_configure(
    config: pytest.Config,
) -> None:
    support.implementation = config.getoption("impl")
    support.cpp_binary = config.getoption("cpp_binary")

    if support.implementation == "cpp":
        binary = support.REPOSITORY_ROOT / support.cpp_binary

        if not binary.is_file():
            raise pytest.UsageError(
                f"--impl cpp: no C++ gateway at {binary}; build it with gateway/cpp/dev.sh build"
            )


def pytest_report_header(
    config: pytest.Config,
) -> str:
    if support.implementation == "cpp":
        return f"gateway implementation: cpp ({support.cpp_binary})"

    return "gateway implementation: python"
