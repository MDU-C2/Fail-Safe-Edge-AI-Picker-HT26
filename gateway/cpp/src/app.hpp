#pragma once

#include <memory>

#include "calibration/service.hpp"
#include "camera/service.hpp"
#include "config/settings.hpp"
#include "control/manager.hpp"
#include "events/bus.hpp"
#include "http/router.hpp"
#include "robot/service.hpp"
#include "rws/client.hpp"
#include "rws/motion.hpp"
#include "security/auth.hpp"
#include "security/client_registry.hpp"
#include "util/time.hpp"

namespace gw {

// ================================================================
// Application
// ================================================================
//
// Everything gateway/main.py wires together: settings, clients,
// the control lease, events, camera, robot and RWS, and the routes.
// Constructing it validates the settings and the clients file and
// throws if either is wrong, so the gateway does not start.
//

class App {
public:
    explicit App(
        Settings settings
    );

    ~App();

    //
    // Starts the camera producer.
    //
    void start();

    //
    // Stops the camera producer and releases the camera.
    //
    void stop();

    const http::Router& router() const {
        return router_;
    }

    Settings settings;

    //
    // Whether clients connect over TLS (GATEWAY_TLS_CERT_FILE set).
    // Without it the gateway only starts in development mode or with
    // GATEWAY_TLS_ALLOW_PLAINTEXT.
    //
    bool tls_enabled;

    ClientRegistry registry;

    ControlManager control;

    Authenticator auth;

    EventBus events;

    CameraService camera;

    std::unique_ptr<CameraProducer> camera_producer;

    CalibrationService calibration;

    std::unique_ptr<RobotService> robot;

    rws::RwsClient rws;

    rws::RwsMotion motion;

    SteadyTime started;

private:
    void add_routes();

    http::Router router_;
};

// ================================================================
// Routes (one function per area, as the Python routers)
// ================================================================

void add_health_routes(App& app, http::Router& router);
void add_control_routes(App& app, http::Router& router);
void add_camera_routes(App& app, http::Router& router);
void add_calibration_routes(App& app, http::Router& router);
void add_event_routes(App& app, http::Router& router);
void add_arm_routes(App& app, http::Router& router);
void add_rapid_routes(App& app, http::Router& router);
void add_motion_routes(App& app, http::Router& router);
void add_robot_routes(App& app, http::Router& router);

}  // namespace gw
