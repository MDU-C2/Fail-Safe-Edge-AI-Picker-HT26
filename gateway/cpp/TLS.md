# TLS for the gateway

Clients (the model tiers, team scripts, diagnostics) talk to the
gateway over TLS: `https://` and `wss://`. The gateway proves who it
is with a certificate from the project's own **lab CA**; clients
trust that CA and nothing else for the gateway. The Bearer tokens of
`auth_clients.json` stay as they are, now inside the encrypted
connection.

The gateway's links to the YuMi (RWS) and the OAK camera stay
unencrypted (RobotWare 6 RWS and DepthAI have no TLS there); they are
protected by keeping them on their own network segment.

## What lives where

Everything private stays on the admin laptop, outside the repository.

| File | Where | Secret? |
|---|---|---|
| `lab-ca.key` | `~/ht26-lab-ca/` on the admin laptop, plus an offline backup | **yes**, encrypted with a passphrase. Whoever has it (and the passphrase) can issue certificates clients accept |
| `lab-ca.crt` | `~/ht26-lab-ca/` and **`gateway/tls/lab-ca.crt` in the repo** | no. Clients trust it |
| `issued/<name>/<name>.key` | `~/ht26-lab-ca/issued/`, and the device that uses it | **yes**. Never in git, never in an image or `.wmopkg` |
| `issued/<name>/<name>.crt` | same | no |
| `lab-ca.srl`, `*.csr`, `*.ext` | `~/ht26-lab-ca/` | no, working files |

`.gitignore` ignores `*.pem`, `*.key`, `*.csr`, `*.srl`, `*.p12` and
`*.pfx`. The old self-signed `gateway/cert.pem` / `key.pem` (CN
`GATEWAY`, no SAN, so clients reject it on the address check) are not
used.

## The tool

[tools/lab_ca.sh](tools/lab_ca.sh) wraps OpenSSL. Run it from the
repository root in **Git Bash** (its OpenSSL 3.5 is new enough; WSL's
Ubuntu 22.04 has 3.0, which lacks `-not_before`).

```bash
gateway/cpp/tools/lab_ca.sh init-ca                       # once
gateway/cpp/tools/lab_ca.sh issue-server NAME SAN [SAN ...]
gateway/cpp/tools/lab_ca.sh show [NAME]                   # dates, SANs, fingerprints
```

| Variable | Default | |
|---|---|---|
| `LAB_CA_DIR` | `~/ht26-lab-ca` | CA directory |
| `LAB_CA_PASSPHRASE` | prompted | CA key passphrase (avoid setting it in shell history) |
| `DAYS` | 395 | validity of an issued certificate (13 months) |
| `CA_DAYS` | 1095 | validity of the CA (3 years) |
| `LAB_CA_PUBLIC` | `gateway/tls/lab-ca.crt` | where `init-ca` copies the public certificate |

What it makes:

- ECDSA P-256 keys (cheap handshakes on the Lynx's ARM CPU).
- The CA: `CA:TRUE, pathlen:0`, and **name constraints**: certificates
  from it are only valid for `169.254.0.0/16`, `192.168.125.0/24`,
  `127.0.0.0/8`, `localhost` and `*.ht26.lab`. A stolen CA key can
  therefore not impersonate any other site. The tool refuses to issue
  outside these and deletes the certificate.
- Server certificates: `serverAuth`, the SANs you give, a **new key
  every time**, `notBefore` three days back so a device whose clock is
  a little behind still accepts a fresh certificate.
- Python 3.13+ verifies strictly by default (critical
  `basicConstraints`, key identifiers); the certificates pass that
  (checked with Python 3.14).

## Create the CA (once)

```bash
gateway/cpp/tools/lab_ca.sh init-ca
```

1. Choose a strong passphrase when OpenSSL asks.
2. Back up `~/ht26-lab-ca/lab-ca.key` together with the passphrase
   (for example on a USB stick kept in the lab, the passphrase in the
   team's password manager). Lose both and you need a new CA, which
   means giving every client the new `lab-ca.crt`.
3. Commit `gateway/tls/lab-ca.crt`.

## Issue the gateway's certificate

The SANs must list **every address clients use** to reach that
gateway; the CN is ignored for this check.

```bash
# Westermo Lynx: link-local, robot subnet, and local tests on the device
gateway/cpp/tools/lab_ca.sh issue-server gateway-lynx \
    IP:169.254.219.127 IP:192.168.125.20 IP:127.0.0.1 DNS:localhost

# a gateway on this laptop (development, WSL)
gateway/cpp/tools/lab_ca.sh issue-server gateway-laptop IP:127.0.0.1 DNS:localhost
```

The output names the key and certificate files and the certificate's
SHA-256 fingerprint. A new address later means issuing again with the
full list.

## Renew

`show` lists the expiry dates. Renew a month ahead:

```bash
gateway/cpp/tools/lab_ca.sh show
gateway/cpp/tools/lab_ca.sh issue-server gateway-lynx IP:169.254.219.127 IP:192.168.125.20 IP:127.0.0.1 DNS:localhost
```

Issuing again moves the previous pair to `issued/<name>/old-<time>/`
and creates a new key. Deploy the new pair; clients need nothing,
since they trust the CA, not the certificate.

**An expired gateway certificate stops every model tier at once** (they
all reject it), so the whole fail-safe chain falls through to the safe
stop. Put the expiry date in the team calendar.

The CA itself lasts three years, longer than the project; renewing it
means a new `lab-ca.crt` for every client.

## When a key leaks

| Leaked | Do |
|---|---|
| A gateway key | Issue that gateway again (new key) and deploy it. The old certificate stays valid until it expires (clients check no revocation lists), so an attacker on the network path could impersonate the gateway until then: keep certificates short-lived |
| The CA key | `init-ca` with a new `LAB_CA_DIR` (or delete the old one), commit the new `gateway/tls/lab-ca.crt`, give it to every client, issue every gateway again |
| A client token | As before: a new token with `gateway/scripts/generate_token.py`, update `auth_clients.json` |

## The gateway

The C++ gateway (0.5.0 and later) serves HTTPS and WSS through
cpp-httplib with OpenSSL; TLS 1.2 is the minimum, renegotiation and
compression are off (cpp-httplib's defaults). One port speaks one
protocol: with a certificate configured, plain `http://` to it fails.

| Setting | |
|---|---|
| `GATEWAY_TLS_CERT_FILE` | the gateway's PEM certificate (`issued/<name>/<name>.crt`) |
| `GATEWAY_TLS_KEY_FILE` | its key, PEM, **unencrypted** (an encrypted key stops startup rather than waiting for a passphrase) |
| `GATEWAY_TLS_ALLOW_PLAINTEXT` | `true` lets strict mode run without TLS, with a warning in the log. Only for a bench or a move to TLS in progress |

Both files or neither. In strict mode without them (and without
`GATEWAY_TLS_ALLOW_PLAINTEXT`) the gateway does not start; a missing
file, a key that is not the certificate's, or an encrypted key stops
it too, before the camera is opened. Development mode runs either
way. `GET /health` reports `"tls": {"enabled": true|false}`.

Running a gateway on this laptop (WSL / Docker) with TLS:

```bash
gateway/cpp/tools/lab_ca.sh issue-server gateway-laptop IP:127.0.0.1 DNS:localhost
GATEWAY_TLS_CERT_FILE=~/ht26-lab-ca/issued/gateway-laptop/gateway-laptop.crt \
GATEWAY_TLS_KEY_FILE=~/ht26-lab-ca/issued/gateway-laptop/gateway-laptop.key \
    gateway/cpp/build/dev/gateway --host 127.0.0.1 --port 8000
```

(In Docker, mount the two files read-only and point the variables at
the mount; never copy them into an image.)

The legacy Python gateway has no TLS settings; uvicorn would take
`--ssl-certfile` / `--ssl-keyfile`, but it is not deployed any more.

## Clients

Clients use `https://…/api/v1` and `wss://…/api/v1/events` and trust
the lab CA, nothing else. The tokens stay as they are.

| Client | How it trusts the lab CA |
|---|---|
| Scripts using `requests` (`gateway/scripts/`, the team's scripts) | no code change: `$env:REQUESTS_CA_BUNDLE = "gateway/tls/lab-ca.crt"` (PowerShell) or `export REQUESTS_CA_BUNDLE=gateway/tls/lab-ca.crt`, and an `https://` URL |
| Python `ssl` / `websockets` (`watch_events.py`) | `SSL_CERT_FILE=gateway/tls/lab-ca.crt`, or `ssl.create_default_context(cafile="gateway/tls/lab-ca.crt")` |
| `diagnostics/gateway_diag.py` | uses `gateway/tls/lab-ca.crt` by itself; `--ca FILE` or `GATEWAY_CA_FILE` for another |
| A model in another language | load `lab-ca.crt` as its only trusted CA for the gateway, and keep host name checking on |

Never turn verification off (`verify=False`, `CERT_NONE`) to make a
connection work: the client would then send its token to anyone in
the path. `gateway_diag.py --insecure` exists to diagnose TLS itself
and still sends the diagnostics token.

The Python `websockets` client over TLS 1.3 occasionally gets no
answer to its opening handshake (about 1 in 300 connections, also
against `websockets`' own server), so a client should reconnect on a
failed connect, as `gateway_diag.py` does. On the laptop's x64 Python
under arm64 Windows it hangs after about 100 TLS WebSocket
connections; run long-lived event clients elsewhere or restart them.

## Tests

The suite's C++ gateways serve TLS with a throwaway CA made per run
by `trustme` (`support.tls_files()`, never the lab CA), so the whole
API is tested over HTTPS / WSS; `gateway/tests/test_tls.py` covers
trust, the host name check, TLS 1.1 being refused, plaintext only
when allowed, and the startup refusals. The legacy Python gateway's
runs stay plain HTTP.

## Deploying on the Lynx

Not done yet. The plan: issue `gateway-lynx` (see above), copy the
key and certificate into the app's `/state` share (`/state/tls/`; the
key never goes into the `.wmopkg`), set `GATEWAY_TLS_CERT_FILE` /
`GATEWAY_TLS_KEY_FILE` on the app, deploy 0.5.0, then point the
clients and `diagnostics`' `lynx` target at `https://`. How to get
the key onto the Lynx without sending it over plain HTTP (the deploy
script serves files over HTTP; scp to the Lynx is off) is still open.

## Later: mTLS

The same CA can issue client certificates
(`lab_ca.sh issue-client <client>`, `clientAuth`). cpp-httplib's
`SSLServer` takes a client CA file, so the gateway would get a
`GATEWAY_TLS_CLIENT_CA_FILE` and pin each client's certificate
fingerprint in `auth_clients.json` next to its token hash. Not
enabled yet.
