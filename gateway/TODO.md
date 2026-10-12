# Gateway TODO

- Pin the versions in `requirements.txt` so every Docker build installs the same packages.
- Move `requests` out of `requirements.txt`: only the `test_*.py` client scripts use it, not the gateway.
