#pragma once

#include <utility>

#include "http/message.hpp"
#include "rws/errors.hpp"
#include "rws/motion.hpp"

namespace gw::rws {

// ================================================================
// Controller errors as gateway responses (rws/errors.py)
// ================================================================
//
// An unreachable controller is 503 with the client's message. The
// controller's own 400/404 are passed on, a refusal because of its
// state (403/409: RAPID running, mastership held elsewhere) becomes
// 409, anything else 502, all as "Robot controller: <reason>".
//
http::HttpError to_http_error(
    const RwsError& error
);

//
// Runs a controller call, turning RwsError into the response above
// and MotionConflict / MotionTimeout into 409 / 504.
//
template <typename Function>
auto call(
    Function&& function
) {
    try {
        return std::forward<Function>(function)();

    } catch (const RwsError& error) {
        throw to_http_error(error);

    } catch (const MotionConflict& error) {
        throw http::HttpError(409, error.what());

    } catch (const MotionTimeout& error) {
        throw http::HttpError(504, error.what());
    }
}

}  // namespace gw::rws
