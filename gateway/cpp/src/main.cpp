// ================================================================
// Robot & Camera Gateway, C++ implementation
// ================================================================
//
//   gateway [--host 127.0.0.1] [--port 8000]
//
// Run from the repository root, like the Python gateway: settings
// come from GATEWAY_* variables and gateway/.env, the clients from
// GATEWAY_AUTH_CLIENTS_FILE (default gateway/auth_clients.json).
//

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>

#include <pthread.h>

#include "app.hpp"
#include "http/server.hpp"
#include "util/log.hpp"
#include "util/text.hpp"

namespace {

struct Arguments {
    std::string host = "127.0.0.1";
    unsigned short port = 8000;
};

[[noreturn]] void usage(
    const char* message
) {
    std::fprintf(stderr, "%s\nusage: gateway [--host HOST] [--port PORT]\n", message);
    std::exit(2);
}

Arguments parse_arguments(
    int argc,
    char** argv
) {
    Arguments arguments;

    for (int i = 1; i < argc; ++i) {
        const std::string name = argv[i];

        if (name == "--help" || name == "-h") {
            usage("Robot & Camera Gateway (C++)");
        }

        if (i + 1 >= argc) {
            usage(("missing value for " + name).c_str());
        }

        const std::string value = argv[++i];

        if (name == "--host") {
            arguments.host = value;
        } else if (name == "--port") {
            const auto port = gw::parse_integer(value);

            if (!port || *port < 0 || *port > 65535) {
                usage(("invalid port " + value).c_str());
            }

            arguments.port = static_cast<unsigned short>(*port);
        } else {
            usage(("unknown option " + name).c_str());
        }
    }

    return arguments;
}

}  // namespace

int main(
    int argc,
    char** argv
) {
    const Arguments arguments = parse_arguments(argc, argv);

    //
    // Block SIGINT/SIGTERM in every thread; the main thread waits
    // for them below and shuts down in order.
    //
    sigset_t signals;

    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGTERM);

    pthread_sigmask(SIG_BLOCK, &signals, nullptr);

    //
    // A client that disconnects mid-write must not kill the process.
    //
    std::signal(SIGPIPE, SIG_IGN);

    // ------------------------------------------------------------
    // Startup: bad settings or a bad clients file stop it here
    // ------------------------------------------------------------

    std::unique_ptr<gw::App> app;

    try {
        app = std::make_unique<gw::App>(gw::load_settings());

        app->start();

    } catch (const std::exception& error) {
        gw::log::error(gw::format("Gateway failed to start: %s", error.what()));

        return 1;
    }

    gw::http::ServerOptions options;

    options.host = arguments.host;
    options.port = arguments.port;

    gw::http::Server server(app->router(), options);

    std::thread([&server]() {
        try {
            server.run();
        } catch (const std::exception& error) {
            gw::log::error(gw::format("Gateway failed to listen: %s", error.what()));

            std::_Exit(1);
        }
    }).detach();

    gw::log::info(
        gw::format(
            "Gateway %s started (security %s, camera %s, robot %s)",
            GATEWAY_VERSION,
            app->settings.security_mode.c_str(),
            app->settings.camera_provider.c_str(),
            app->settings.robot_provider.c_str()
        )
    );

    // ------------------------------------------------------------
    // Shutdown
    // ------------------------------------------------------------

    int received = 0;

    sigwait(&signals, &received);

    gw::log::info(gw::format("Shutting down (%s)", strsignal(received)));

    //
    // Release the camera, then leave without unwinding: connection
    // threads may still be running and hold references into app.
    //
    app->stop();

    std::fflush(stdout);
    std::_Exit(0);
}
