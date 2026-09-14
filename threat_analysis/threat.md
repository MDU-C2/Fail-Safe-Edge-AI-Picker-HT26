# Threat Analysis - Fail-Safe Edge AI Picker

## Overview

This folder contains the threat model for the Fail-Safe Edge AI Picker system.

The system combines cloud AI, edge AI, and local fallback AI models to control a robotic dish-handling platform through a centralized Robot Gateway. The architecture is designed around fail-safe principles, ensuring continued operation during network outages, compute failures, or degraded system conditions.

The threat model is maintained using OWASP Threat Dragon and follows the STRIDE methodology.

---

## Architecture Summary

The system consists of:

- RGB-D Camera
- Cloud AI Model (Primary)
- Raspberry Pi AI Model (Secondary)
- Lynx AI Model (Tertiary)
- Robot Gateway
- ABB YuMi Robot

The Robot Gateway acts as the sole trusted command path between AI decision-making components and the robot. All AI-generated commands must pass through the gateway before execution.

---

## Repository Structure

```text
.
├── threat-model.json        # Threat Dragon source file
├── threat_model.pdf         # Exported threat report
├── diagrams/
│   └── architecture.png
└── README.md
```

---

## Threat Modeling Workflow

This project follows an iterative threat-modeling process:

1. Update the system architecture.
2. Update the Threat Dragon model.
3. Add, modify, or review threats.
4. Assign STRIDE categories.
5. Review severity and mitigations.
6. Export updated reports.

### Source of Truth

The Threat Dragon JSON file is the authoritative source for the threat model.

```text
threat-model.json
```

Generated files such as PDFs should be treated as artifacts generated from the JSON model and should not be edited manually.

---

## STRIDE Methodology

Threats are categorized according to the STRIDE framework.

| Category | Description |
|-----------|-------------|
| Spoofing | Impersonation of a trusted entity |
| Tampering | Unauthorized modification of data or behavior |
| Repudiation | Inability to verify actions or accountability |
| Information Disclosure | Exposure of sensitive information |
| Denial of Service | Loss or degradation of system availability |
| Elevation of Privilege | Unauthorized gain of permissions or access |

---

## Security Architecture Principles

### Single Trusted Control Path

The Robot Gateway is the only approved path for robot commands.

### Independent Command Validation

AI-generated outputs are treated as untrusted until verified by the Robot Gateway.

### Fail-Safe Operation

The control hierarchy automatically degrades through available compute resources:

```text
Cloud AI
    ↓
Raspberry Pi AI
    ↓
Lynx AI
    ↓
Safe Stop
```

### Network Segmentation

The robot, gateway, camera, and AI services operate within defined trust boundaries and communicate only through approved channels.

---

## Working with the Threat Model

### Adding a New Threat

When adding a threat in Threat Dragon:

1. Select the relevant process or data flow.
2. Choose the applicable STRIDE category.
3. Assign a severity:
   - Critical
   - High
   - Medium
   - Low
   - TBD
4. Add a clear threat description.
5. Document mitigation measures.
6. Verify the threat is not duplicated elsewhere in the model.

### Threat Placement Guidelines

#### Process Nodes

Use process nodes for threats affecting:

- Cloud AI Model
- Raspberry Pi Model
- Lynx Model
- Robot Gateway
- Camera
- Robot

Examples:

- Spoofing
- Elevation of Privilege
- Unsafe Command Generation
- Configuration Tampering

#### Data Flows

Use connections for threats affecting communication paths.

Available STRIDE categories:

- Tampering
- Information Disclosure
- Denial of Service

Examples:

- Modified Instructions in Transit
- Intercepted Communications
- Loss of Connectivity

---

## Current Model Scope

The threat model currently covers:

- RGB-D Camera
- Cloud AI Processing
- Raspberry Pi Edge Processing
- Lynx Local Processing
- Robot Gateway
- ABB YuMi Robot
- Inter-component Data Flows
- Trust Boundaries
- Failover Mechanisms

---

## Artifacts

The following artifacts may be generated from the model:

- Threat Dragon JSON
- PDF Threat Reports
- Architecture Diagrams
- Security Documentation

Always update the Threat Dragon model first and regenerate artifacts afterward.

---

## Future Work

- Refine threat likelihood ratings
- Expand mitigations
- Validate assumptions against implementation
- Review threats after architectural changes
- Perform mitigation verification and testing