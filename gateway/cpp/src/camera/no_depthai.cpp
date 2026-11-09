// Built instead of depthai_device.cpp when depthai-core is not used
// (GATEWAY_WITH_DEPTHAI=OFF): the gateway then has only the mock camera.

#include "camera/oak_device.hpp"

namespace gw {

std::unique_ptr<OakConnector> create_depthai_connector(
    const OakConfig&
) {
    return nullptr;
}

}  // namespace gw
