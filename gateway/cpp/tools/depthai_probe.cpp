// RAM probe for depthai-core without a camera: what the host side
// costs before any device is connected, stage by stage.
//
//     gateway/cpp/dev.sh shell, then
//     cmake --build gateway/cpp/build/dev --target depthai_probe
//     gateway/cpp/build/dev/depthai_probe [search]
#include <malloc.h>

#include <chrono>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

#include <depthai/depthai.hpp>

static void report(const char* stage) {
    std::ifstream status("/proc/self/status");
    std::string line, rss, anon, file;
    while(std::getline(status, line)) {
        if(line.rfind("VmRSS:", 0) == 0) rss = line.substr(6);
        if(line.rfind("RssAnon:", 0) == 0) anon = line.substr(8);
        if(line.rfind("RssFile:", 0) == 0) file = line.substr(8);
    }
    auto info = mallinfo2();
    std::printf("%-34s RSS %s | anon %s | file %s | heap in use %6.1f MB, free %6.1f MB, mmapped %6.1f MB\n",
                stage, rss.c_str(), anon.c_str(), file.c_str(),
                info.uordblks / 1048576.0, info.fordblks / 1048576.0, info.hblkhd / 1048576.0);
}

int main(int argc, char** argv) {
    report("start");

    dai::initialize();
    report("dai::initialize()");

    std::this_thread::sleep_for(std::chrono::seconds(3));
    report("after lazy firmware unpack (3 s)");

    {
        dai::Device::Config config;
        auto firmware = dai::Device::getEmbeddedDeviceBinary(config);
        std::printf("  device firmware (boot image): %.1f MB\n", firmware.size() / 1048576.0);
        report("holding boot image");
    }
    report("boot image freed");

    {
        auto bootloader = dai::DeviceBootloader::getEmbeddedBootloaderBinary(dai::bootloader::Type::NETWORK);
        std::printf("  bootloader (network): %.1f MB\n", bootloader.size() / 1048576.0);
    }

    malloc_trim(0);
    report("after malloc_trim");

    if(argc > 1) {
        // Look for a device as the gateway would (no device needed to measure).
        auto devices = dai::Device::getAllAvailableDevices();
        std::printf("  devices found: %zu\n", devices.size());
        report("after device search");
    }
    return 0;
}
