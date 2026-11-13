#pragma once

// ================================================================
// cpp-httplib: HTTP, WebSockets and TLS for the server
// ================================================================
//
// The pinned copy from cmake/Dependencies.cmake, included only from
// http/server.cpp and only through this header.
//
// It is compiled in under the namespace gw_httplib: depthai-core
// links its own, older httplib (0.20.1) into the same binary, and
// under one name the linker would mix the two versions' inline
// functions.
//

#define CPPHTTPLIB_OPENSSL_SUPPORT

//
// Largest WebSocket message from a client (subscription requests are
// small).
//
#define CPPHTTPLIB_WEBSOCKET_MAX_PAYLOAD_LENGTH 65536

#define httplib gw_httplib

#include <gw_httplib/httplib.h>
