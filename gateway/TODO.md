# Gateway TODO

- Pin the versions in `requirements.txt` so every Docker build installs the same packages.
- Move `requests` out of `requirements.txt`: only the `test_*.py` client scripts use it, not the gateway.
- Keep `gateway/.env`, `gateway/auth_clients.json` and the calibration file outside the container: mount them from the host (for example `-v` on the Pi) instead of baking `.env` and `auth_clients.json` into the image, so config and tokens can change without a rebuild and the calibration survives a redeploy.
