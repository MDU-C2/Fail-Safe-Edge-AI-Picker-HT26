include(GoogleTest)

add_executable(
    gateway_tests
    test_client_registry.cpp
    test_control_manager.cpp
    test_events.cpp
    test_router.cpp
    test_rws_errors.cpp
    test_settings.cpp
    test_util.cpp
)

target_link_libraries(
    gateway_tests
    PRIVATE
    gateway_core
    GTest::gtest_main
)

#
# From the repository root, like the gateway, so the tests can read
# gateway/auth_clients.json when it is there.
#
gtest_discover_tests(
    gateway_tests
    WORKING_DIRECTORY ${PROJECT_SOURCE_DIR}/../..
    DISCOVERY_MODE PRE_TEST
)
