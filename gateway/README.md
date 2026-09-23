# Robot & Camera Gateway

A Python gateway for securely connecting multiple external systems to a robot and its cameras.

The gateway provides one controlled entry point for robot commands, camera data, status information, and asynchronous events. Multiple clients may connect at the same time, but only one client may have control authority over the robot and camera controls at any given time.

> **Project status:** Initial architecture / development. The security model is designed in from the beginning, but the first implementation may use development-only relaxed security while the system is being built.

## Goals

The gateway is intended to support:

- Multiple simultaneously connected client systems.
- JSON-based commands and requests.
- RGB image retrieval.
- Depth image retrieval.
- Robot status retrieval.
- Asynchronous robot and gateway notifications.
- Authentication and authorization of clients.
- Encrypted communication in production.
- Exactly one active controller at a time.
- Defined control priority between clients.
- Automatic loss of control when a controller disappears.
- Validation of commands before they are forwarded to the robot.
- A development mode in which encryption/authentication can intentionally be relaxed without changing the main architecture.

## Architecture

```text
 Client A ---------\
 Client B ----------+---- HTTPS / WSS ----+
 Client C ---------/                       |
                                             v
                                  +-----------------------+
                                  |        Gateway        |
                                  |                       |
                                  | Authentication        |
                                  | Authorization         |
                                  | Control Manager       |
                                  | Command Validation    |
                                  | Camera Manager        |
                                  | Event Manager         |
                                  +-----------+-----------+
                                              |
                                      verified commands
                                              |
                                              v
                                         +---------+
                                         |  Robot  |
                                         +---------+
```

The gateway is the authority between network clients and the robot. Network clients should not be able to bypass the gateway's authentication, control-ownership, validation, and safety logic.

## Communication model

Different kinds of traffic have different requirements. The project therefore intentionally separates normal HTTP requests, binary camera data, and asynchronous events.

### REST API over HTTP/HTTPS

REST endpoints are used for request/response operations such as:

- acquiring or releasing control;
- obtaining robot status;
- submitting commands;
- requesting the latest RGB image;
- requesting the latest depth image.

Example conceptual endpoints:

```text
GET    /api/v1/health

GET    /api/v1/control
POST   /api/v1/control
DELETE /api/v1/control

GET    /api/v1/robot/status
POST   /api/v1/robot/command

GET    /api/v1/camera/status
GET    /api/v1/camera/rgb
GET    /api/v1/camera/depth

WSS    /api/v1/events
```

The exact API is expected to evolve as the robot interface is implemented.

### JSON commands

Commands should use well-defined schemas rather than arbitrary JSON being forwarded directly to the robot.

Example:

```json
{
  "command": "move",
  "parameters": {
    "hand": "right",
    "x": 1.0,
    "y": 0.0,
    "z": 0.5
  }
}
```

A command should pass through several checks before reaching the robot:

```text
Request
  |
  v
Authenticated?
  |
  v
Authorized?
  |
  v
Owns control lease?
  |
  v
Schema valid?
  |
  v
Command valid and safe?
  |
  v
Forward to robot
```

### Camera data

Camera images should normally be returned as binary data, not embedded as Base64 in JSON.

For example, an RGB endpoint may return JPEG or PNG data directly:

```text
GET /api/v1/camera/rgb
Content-Type: image/jpeg
```

Depth images need a defined representation. Possible formats include a 16-bit depth image or a binary array containing depth values. The final representation should be chosen to match the camera and consumer requirements.

Continuous high-frame-rate video is deliberately not part of the initial API design. If continuous RGB/depth streaming is required later, a dedicated streaming transport can be introduced rather than forcing video through the event WebSocket.

### WebSocket events

Clients need to receive information that originates at the robot without polling continuously. A persistent WebSocket connection is used for this.

```text
wss://gateway/api/v1/events
```

Example events:

```json
{
  "type": "robot.status",
  "data": {
    "state": "moving"
  }
}
```

```json
{
  "type": "control.changed",
  "data": {
    "controller": "operator"
  }
}
```

```json
{
  "type": "control.revoked",
  "data": {
    "reason": "higher_priority_controller"
  }
}
```

Eventually clients can subscribe only to relevant event groups, for example robot status, errors, control changes or camera status.

## Authentication, authorization and control

These are intentionally separate concepts.

### Authentication

Authentication answers:

> Who is this client?

Each connected system should have a distinct identity. Credentials must not be hard-coded into the source repository.

The initial implementation can use application credentials or tokens. The architecture should allow stronger machine-to-machine authentication such as mutual TLS (mTLS) to be added later.

### Authorization

Authorization answers:

> What is this client allowed to do?

Example permissions could include:

```text
robot.read
robot.control
camera.read
camera.control
```

A read-only monitoring client can therefore remain connected and receive camera/status information without being allowed to command the robot.

Authorization must be checked server-side. A client claiming that it has a permission is never sufficient.

### Control ownership

Authorization to control a robot does not mean that the client currently owns control.

Only one client can own the active control lease at a time.

Example priority configuration:

```text
Priority 100    Operator
Priority 50     Autonomous controller
Priority 10     Diagnostic controller
```

A higher-priority client may preempt a lower-priority controller. A lower-priority client cannot take control from a higher-priority controller.

For the first version, robot and camera control are treated as a single control lease. Camera *reading* can still be allowed for other clients. This can be separated into independent leases later if a real requirement appears.

### Control lease and timeout

Control should not remain assigned forever when a client loses power or connectivity.

The active controller therefore owns a time-limited lease and periodically renews it.

```text
Controller acquires lease
        |
        v
Controller periodically renews
        |
        +---- connection remains healthy ----+
        |                                     |
        +---- renewals stop                   |
              |
              v
          lease expires
              |
              v
       control is revoked
              |
              v
       robot enters the defined safe state
```

The lease duration and renewal interval will be configurable. The robot's exact safe-state action must be defined together with the physical robot interface and its safety requirements.

## Security model

Security is part of the architecture from the beginning, even while the initial development configuration is relaxed.

Production goals include:

- TLS-encrypted HTTPS connections.
- Secure WebSockets (`wss://`).
- Authentication for every client.
- Authorization at protected endpoints.
- Distinct identities for client systems.
- Input/schema validation.
- Command validation before robot forwarding.
- Request rate limits.
- Maximum body/image/message sizes.
- Audit logging of commands and control changes.
- Credentials stored outside source control.
- Suitable certificate/key handling.
- Optional future mTLS for strong machine-to-machine authentication.

### Development mode

An intentionally insecure development mode can allow HTTP/WS and a development identity so that the rest of the system can be built before certificates and production authentication are configured.

Conceptually:

```text
SECURITY_MODE=development
```

versus:

```text
SECURITY_MODE=strict
```

The important rule is that development mode replaces the *security provider*, rather than bypassing authorization checks throughout the application.

```text
Request
   |
   v
Security Provider
   |
   +-- DevelopmentSecurity
   |
   +-- ProductionSecurity
   |
   v
Identity + permissions
   |
   v
Application
```

Never expose development/unsecured mode to an untrusted network or the public Internet.

## Safety boundary

Authentication is not robot safety.

Even an authenticated highest-priority controller should not be able to forward arbitrary unchecked data directly to the robot.

The system should keep these responsibilities distinct:

```text
Authentication -> Who are you?
Authorization  -> What are you allowed to do?
Control lease  -> Are you currently in charge?
Validation     -> Is the command structurally valid?
Safety         -> Is the command permissible right now?
```

Robot-specific limits, emergency-stop behavior, watchdogs, physical interlocks and other safety-critical mechanisms must ultimately be defined for the real robot. Network/API controls should not be treated as a replacement for appropriate independent safety mechanisms.

## Suggested project structure

As the implementation grows, use a structure similar to:

```text
gateway/
|-- main.py
|-- config.py
|
|-- security/
|   |-- authentication.py
|   |-- authorization.py
|   `-- identity.py
|
|-- control/
|   `-- manager.py
|
|-- robot/
|   |-- router.py
|   `-- service.py
|
|-- camera/
|   |-- router.py
|   `-- service.py
|
|-- events/
|   |-- router.py
|   `-- manager.py
|
`-- models/
    |-- commands.py
    |-- status.py
    `-- events.py
```

`main.py` should create the web application and register routers. Robot-control behavior should live in services rather than HTTP route functions, making the actual robot transport replaceable and testable.

## Initial implementation

The current minimal FastAPI server can start with the following dependencies:

```text
fastapi
uvicorn[standard]
```

A minimal project can initially look like:

```text
project/
|-- server.py
|-- requirements.txt
|-- cert.pem       # local TLS certificate, not committed if private/sensitive
`-- key.pem        # private key, NEVER commit
```

As features are implemented, migrate toward the modular `gateway/` layout above.

## Prerequisites

Install Python 3.10 or newer. A virtual environment is strongly recommended.

Check Python:

```bash
python --version
```

Depending on the operating system, the executable may be named `python3` instead.

## Installation

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

With the virtual environment active:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

The initial `requirements.txt` is:

```text
fastapi
uvicorn[standard]
```

## Running during early development

For local development, the easiest setup is plain HTTP bound only to localhost:

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

Then open:

```text
http://127.0.0.1:8000/
```

FastAPI also provides interactive API documentation by default at:

```text
http://127.0.0.1:8000/docs
```

`--reload` is intended for development only.

If another computer on the trusted development network must reach the service, it can be bound to all interfaces:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

Do not treat that as secure merely because it is on a local network. Configure the host firewall appropriately and do not expose an unsecured development server to the Internet.

## Running with HTTPS directly in Uvicorn

For an initial TLS-enabled deployment, Uvicorn can load a certificate and private key directly:

```bash
uvicorn server:app \
  --host 0.0.0.0 \
  --port 8443 \
  --ssl-keyfile key.pem \
  --ssl-certfile cert.pem
```

On Windows PowerShell, the equivalent can be entered on one line:

```powershell
uvicorn server:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

The API is then available at:

```text
https://SERVER_ADDRESS:8443
```

A self-signed certificate is useful for controlled development/testing but will not be trusted automatically by normal clients. Production certificates should be managed appropriately for the deployment environment.

## Creating a development self-signed certificate

If OpenSSL is installed, a simple development certificate can be generated with:

```bash
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout key.pem \
  -out cert.pem \
  -days 365
```

The generated `key.pem` is private. Do not commit it to Git or distribute it unnecessarily.

For real deployment, certificate issuance, renewal, hostnames and trust should be designed for the actual network environment rather than relying on a self-signed development certificate.

## Production TLS layout

A later production deployment may terminate TLS in a dedicated reverse proxy rather than inside Python:

```text
Clients
   |
   | HTTPS/WSS :443
   v
+-------------------+
| TLS reverse proxy |
| Caddy/Nginx/etc.  |
+---------+---------+
          |
          | private/local HTTP
          v
+-------------------+
| FastAPI / Uvicorn |
+-------------------+
```

This is a deployment choice rather than an application architecture change. Authentication, authorization, control ownership and command validation still belong in the gateway.

## Example command request

Once the command endpoint exists, a request will conceptually look like:

```http
POST /api/v1/robot/command
Content-Type: application/json
Authorization: Bearer <credential>
```

```json
{
  "command": "move",
  "parameters": {
    "x": 1.0,
    "y": 0.0
  }
}
```

The server should reject the command if the caller is unauthenticated, lacks `robot.control`, does not own the active control lease, sends an invalid schema, or fails robot-specific validation/safety checks.

## Configuration

Runtime behavior should ultimately be configured using environment variables or a configuration file rather than hard-coded values.

Expected configuration subjects include:

```text
SECURITY_MODE
HOST
PORT
TLS_CERT_FILE
TLS_KEY_FILE
CONTROL_LEASE_SECONDS
MAX_REQUEST_SIZE
MAX_IMAGE_SIZE
LOG_LEVEL
```

Secrets and private keys must not be committed to source control.

A future `.gitignore` should at minimum cover local virtual environments, Python caches, environment secret files and development private keys, for example:

```gitignore
.venv/
__pycache__/
*.py[cod]
.env
key.pem
```

Do not blindly ignore a certificate/key path if production deployment tooling needs to manage it differently. Private keys should never live in Git.

## Testing approach

The gateway should eventually have automated tests for at least:

- unauthenticated access is rejected in strict mode;
- read-only clients cannot command the robot;
- a controller can acquire and renew its lease;
- only one controller owns control at a time;
- lower-priority takeover is rejected;
- higher-priority takeover works as defined;
- revoked clients can no longer issue commands;
- lease expiry revokes control;
- disconnect/timeout produces the intended safe behavior;
- malformed JSON is rejected;
- unknown commands are rejected;
- command limits are validated;
- large requests are rejected;
- RGB/depth responses use the expected format;
- event subscribers receive only permitted/relevant events;
- development mode and strict mode exercise the same application authorization model.

## Initial development roadmap

### Phase 1: API skeleton

- Create the FastAPI project structure.
- Add configuration handling.
- Add `/health`.
- Define typed request/response models.
- Implement development security provider.

### Phase 2: Client and control model

- Add identities and permissions.
- Add acquire/release control.
- Add priority arbitration.
- Add lease renewal and expiry.
- Add control-change events.

### Phase 3: Robot interface

- Define a robot service/interface independent of HTTP.
- Validate commands.
- Add a mock robot for development.
- Connect the real transport only after the boundary is stable.

### Phase 4: Camera interface

- Define RGB output format.
- Define depth output format.
- Add current-frame endpoints.
- Add size/rate protections.

### Phase 5: Events

- Add authenticated WebSocket connections.
- Add subscriptions.
- Publish robot status/errors and control changes.
- Handle disconnects and slow clients.

### Phase 6: Production security

- Require HTTPS/WSS.
- Introduce real client credentials and credential rotation.
- Add endpoint permissions and audit logging.
- Add rate/size limits.
- Evaluate and, if appropriate, implement mTLS.
- Harden production deployment and disable unsafe development behavior.

## Design decisions still to make

The following are deliberately unresolved until the hardware/network requirements are clearer:

1. Exact authentication mechanism for the first secured deployment.
2. Whether mTLS will be mandatory for every machine client.
3. Exact control priority list and whether every higher-priority client may preempt immediately.
4. Lease/heartbeat timings.
5. Robot behavior when control is lost.
6. RGB image format and desired quality/resolution.
7. Depth image datatype, units and encoding.
8. Whether clients request snapshots or need continuous camera streaming.
9. Which events each permission level may subscribe to.
10. Robot transport used behind the gateway.
11. Persistence requirements for audit logs and configuration.
12. Production deployment model and certificate management.

## Core design principle

Keep the network layer separate from the robot implementation:

```text
FastAPI route
     |
     v
Application service
     |
     v
Authorization + control + validation
     |
     v
Robot/camera interface
     |
     v
Physical system
```

This lets the gateway evolve from a simple Python development server into a secured robot gateway without rewriting the fundamental control model.
