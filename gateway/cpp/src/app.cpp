#include "app.hpp"

#include <stdexcept>

#include "camera/mock_producer.hpp"

namespace gw::embedded {

extern const std::string_view openapi_json;

}  // namespace gw::embedded

namespace gw {

namespace {

std::unique_ptr<CameraProducer> create_camera_producer(
    const Settings& settings,
    CameraService& camera
) {
    if (settings.camera_provider == "mock") {
        return std::make_unique<MockCameraProducer>(camera);
    }

    throw std::runtime_error(
        "Camera provider 'oak' is not available in this build yet; use GATEWAY_CAMERA_PROVIDER=mock"
    );
}

}  // namespace

App::App(
    Settings settings_value
)
    : settings(std::move(settings_value)),
      registry(settings.auth_clients_file),
      control(
          settings.control_lease_seconds,
          settings.control_contention_seconds,
          settings.control_startup_grace_seconds
      ),
      auth(registry, settings.security_mode == "development", control),
      camera(events),
      camera_producer(create_camera_producer(settings, camera)),
      calibration(settings.calibration_file),
      robot(create_robot_service(settings)),
      rws(
          settings.robot_host,
          settings.rws_port,
          settings.rws_user,
          settings.rws_password,
          settings.rws_timeout
      ),
      motion(
          rws,
          settings.rws_move_timeout,
          settings.rws_safe_zone_min,
          settings.rws_safe_zone_max,
          rws::MotionSpeed{
              settings.rws_default_speed,
              settings.rws_default_acceleration,
              settings.rws_default_ramp,
          }
      ),
      started(std::chrono::steady_clock::now()) {
    add_routes();
}

App::~App() {
    stop();
}

void App::start() {
    camera_producer->start();
}

void App::stop() {
    if (camera_producer) {
        camera_producer->stop();
    }
}

void App::add_routes() {
    //
    // FastAPI serves the schema outside the API prefix.
    //
    router_.get(
        "/openapi.json",
        [](http::Request&) {
            http::Response response;

            response.body = std::string(embedded::openapi_json);

            return response;
        }
    );

    add_health_routes(*this, router_);
    add_control_routes(*this, router_);
    add_robot_routes(*this, router_);
    add_event_routes(*this, router_);
    add_camera_routes(*this, router_);
    add_calibration_routes(*this, router_);
    add_arm_routes(*this, router_);
    add_rapid_routes(*this, router_);
    add_motion_routes(*this, router_);
}

}  // namespace gw
