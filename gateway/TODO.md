# Gateway TODO

- Pin the versions in `requirements.txt` so every Docker build installs the same packages.
- Keep `gateway/.env`, `gateway/auth_clients.json` and the calibration file outside the container: mount them from the host (for example `-v` on the Pi) instead of baking `.env` and `auth_clients.json` into the image, so config and tokens can change without a rebuild and the calibration survives a redeploy.
- RWS endpoints (`gateway/rws/`): the gateway does not watch the controller, so `robot.rapid.*` events only fire for actions taken through the gateway, and a lapsed control lease does not stop RAPID. `/robot/status` still comes from the TCP robot service, not RWS.
