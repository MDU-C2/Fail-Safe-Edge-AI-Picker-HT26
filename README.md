# Fail-safe edge-AI picker

Project repository for the **DVA490 / DVA474 – Projektkurs i Robotik** course at Mälardalen University (MDU), autumn semester 2026 (HT2026).

Course center: [Projects @ Collaborative Center (C2) MDU](https://github.com/MDU-C2)

## Robot gateway

The gateway that every AI tier drives the YuMi and the OAK camera
through is the **C++ gateway** in [gateway/cpp/](gateway/cpp/README.md),
running on the Westermo Lynx ([gateway/cpp/LYNX.md](gateway/cpp/LYNX.md)).
The Python gateway in [gateway/](gateway/README_START.md) is **legacy**:
kept as the reference implementation and for its test suite, but no
longer updated. New gateway work goes into the C++ one.
