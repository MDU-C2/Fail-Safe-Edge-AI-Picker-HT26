#pragma once

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <climits>
#include <poll.h>

#include <boost/asio/error.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/system/system_error.hpp>

namespace gw::http {

// Synchronous Asio retries EAGAIN with an unbounded poll, so SO_RCVTIMEO
// does not bound its blocking reads. Keep the socket nonblocking and wait
// here instead. The deadline applies only while transferring bytes; a robot
// request handler may take as long as its existing completion rules allow.
class IdleDeadlineStream {
public:
    IdleDeadlineStream(
        boost::asio::ip::tcp::socket& socket,
        std::chrono::milliseconds idle_timeout
    ) : socket_(socket), idle_timeout_(idle_timeout) {
        socket_.non_blocking(true);
    }

    void restore_blocking() {
        socket_.non_blocking(false);
    }

    template <typename MutableBufferSequence>
    std::size_t read_some(
        const MutableBufferSequence& buffers,
        boost::system::error_code& error
    ) {
        const auto deadline = Clock::now() + idle_timeout_;
        while (true) {
            const auto count = socket_.read_some(buffers, error);
            if (!would_block(error) || !wait(POLLIN, deadline, error)) {
                return count;
            }
        }
    }

    template <typename ConstBufferSequence>
    std::size_t write_some(
        const ConstBufferSequence& buffers,
        boost::system::error_code& error
    ) {
        const auto deadline = Clock::now() + idle_timeout_;
        while (true) {
            const auto count = socket_.write_some(buffers, error);
            if (!would_block(error) || !wait(POLLOUT, deadline, error)) {
                return count;
            }
        }
    }

    template <typename MutableBufferSequence>
    std::size_t read_some(const MutableBufferSequence& buffers) {
        boost::system::error_code error;
        const auto count = read_some(buffers, error);
        if (error) {
            throw boost::system::system_error(error);
        }
        return count;
    }

    template <typename ConstBufferSequence>
    std::size_t write_some(const ConstBufferSequence& buffers) {
        boost::system::error_code error;
        const auto count = write_some(buffers, error);
        if (error) {
            throw boost::system::system_error(error);
        }
        return count;
    }

private:
    using Clock = std::chrono::steady_clock;

    static bool would_block(const boost::system::error_code& error) {
        return error == boost::asio::error::would_block
            || error == boost::asio::error::try_again;
    }

    bool wait(short events, Clock::time_point deadline, boost::system::error_code& error) {
        while (true) {
            const auto remaining = deadline - Clock::now();
            if (remaining <= Clock::duration::zero()) {
                error = boost::asio::error::timed_out;
                return false;
            }
            const auto milliseconds = std::chrono::ceil<std::chrono::milliseconds>(remaining).count();
            pollfd descriptor{socket_.native_handle(), events, 0};
            const int result = ::poll(&descriptor, 1, static_cast<int>(std::min<long long>(milliseconds, INT_MAX)));
            if (result > 0) {
                // EOF and reset are reported by the next socket operation.
                // A closed/invalid descriptor must not become a busy loop.
                if (descriptor.revents & POLLNVAL) {
                    error = boost::asio::error::bad_descriptor;
                    return false;
                }
                return true;
            }
            if (result == 0) {
                error = boost::asio::error::timed_out;
                return false;
            }
            if (errno != EINTR) {
                error.assign(errno, boost::system::system_category());
                return false;
            }
        }
    }

    boost::asio::ip::tcp::socket& socket_;
    std::chrono::milliseconds idle_timeout_;
};

}  // namespace gw::http
