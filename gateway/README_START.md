# Gateway starter

## Install

```bash
python -m venv .venv
```

Activate the environment, then:

```bash
python -m pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env`. For the starter, keep:

```text
GATEWAY_SECURITY_MODE=development
```

Development mode intentionally grants a local development identity all application permissions. Do not expose it to an untrusted network.

## Run

From the directory containing the `gateway` folder:

```bash
uvicorn gateway.main:app --host 127.0.0.1 --port 8000 --reload
```

Open:

- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/api/v1/health
- Robot status: http://127.0.0.1:8000/api/v1/robot/status

Example mock command:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/robot/command -H 'Content-Type: application/json' -d '{"command":"hello","parameters":{}}'
```

The robot service is a mock and does not contact physical hardware. The next implementation phase is control ownership, priority, lease renewal/expiration, and event notification.
