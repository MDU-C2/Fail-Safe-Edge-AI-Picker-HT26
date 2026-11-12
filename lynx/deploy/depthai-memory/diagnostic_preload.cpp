// OFFLINE DIAGNOSTIC ONLY. Never package or preload this on the robot gateway.
// Executes the pinned wheel's unchanged reader, then precisely the source
// patch's extra shrink_to_fit(), to isolate its RSS effect during a long build.
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <dlfcn.h>
#include <vector>

constexpr auto symbol = "_ZN3dai7utility11ArchiveUtil9readEntryEP13archive_entryRSt6vectorIhSaIhEE";
extern "C" void archive_capacity_diagnostic(void*, void*, std::vector<uint8_t>&)
    __asm__("_ZN3dai7utility11ArchiveUtil9readEntryEP13archive_entryRSt6vectorIhSaIhEE");

extern "C" void archive_capacity_diagnostic(void* self, void* entry, std::vector<uint8_t>& output) {
    using Original = void (*)(void*, void*, std::vector<uint8_t>&);
    static auto original = reinterpret_cast<Original>(dlsym(RTLD_NEXT, symbol));
    if(!original) {
        std::fputs("Cannot resolve original DepthAI archive reader; diagnostic aborted\n", stderr);
        std::abort();
    }
    original(self, entry, output);
    const auto before = output.capacity();
    output.shrink_to_fit();
    std::fprintf(stderr, "archive_capacity size=%zu before=%zu after=%zu\n",
                 output.size(), before, output.capacity());
}
