#!/usr/bin/env python3
"""Compile the two actual readEntry bodies and compare real archive entries.

Run in Dockerfile.builder with an upstream checkout containing only our patch.
This never imports DepthAI or opens a camera. The C++ harness uses libarchive,
and only replaces error-message formatting outside the code under test.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

COMMIT = "89fd1e837fe1c64e8250036ccc6ec7fcd706ad6e"


def body(text):
    start = text.index("void ArchiveUtil::readEntry(struct archive_entry*")
    end = text.index("\nbool ArchiveUtil::readEntry", start)
    return text[start:end].replace("ArchiveUtil::", "Reader::", 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive", type=Path, action="append", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    original = subprocess.check_output(
        ["git", "show", f"{COMMIT}:src/utility/ArchiveUtil.cpp"],
        cwd=args.source, text=True,
    )
    patched = (args.source / "src/utility/ArchiveUtil.cpp").read_text()
    before, after = body(original), body(patched)
    assert "out.shrink_to_fit();" not in before
    assert after.count("out.shrink_to_fit();") == 1

    # Boundary sizes catch empty entries and EOF behavior. A non-seekable ZIP
    # supplies streaming entries whose lengths may be unknown at the first read.
    entries = {f"entry-{n}": bytes(range(256)) * (n // 256) + bytes(range(n % 256))
               for n in (0, 1, 16383, 16384, 16385, 32768, 28757336)}
    synthetic = args.output / "boundaries.tar.xz"
    with tarfile.open(synthetic, "w:xz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    class Stream(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *unused):
            raise io.UnsupportedOperation("stream")

    stream = Stream()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    streaming = args.output / "boundaries.zip"
    streaming.write_bytes(stream.getvalue())
    source = r'''
#include <archive.h>
#include <archive_entry.h>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>
namespace fmt { template<typename... T> std::string format(T&&...) { return "archive read error"; } }
#define DAI_CHECK_IN(condition) do { if(!(condition)) throw std::runtime_error("null archive"); } while(0)
#define DAI_CHECK(condition, message) do { if(!(condition)) throw std::runtime_error(message); } while(0)
'''
    for name, function in (("upstream", before), ("patched", after)):
        source += (f"\nnamespace {name} {{\nstruct Reader {{ archive* aPtr; "
                   "void readEntry(archive_entry*, std::vector<uint8_t>&); };\n"
                   + function + "\n}\n")
    source += r'''
archive* open_archive(const char* path) {
    auto* a = archive_read_new();
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);
    if(archive_read_open_filename(a, path, 16384) != ARCHIVE_OK)
        throw std::runtime_error("cannot open fixture");
    return a;
}
int main(int argc, char** argv) {
    size_t total_size=0, old_capacity=0, new_capacity=0, count=0;
    for(int file=1;file<argc;++file) {
        auto* a = open_archive(argv[file]); auto* b = open_archive(argv[file]);
        archive_entry *ea=nullptr, *eb=nullptr;
        upstream::Reader old_reader{a}; patched::Reader new_reader{b};
        // Reuse a vector across a large entry followed by a small entry, too.
        std::vector<uint8_t> reused;
        while(archive_read_next_header(a,&ea)==ARCHIVE_OK) {
            if(archive_read_next_header(b,&eb)!=ARCHIVE_OK) return 2;
            std::vector<uint8_t> old_bytes;
            old_reader.readEntry(ea,old_bytes); new_reader.readEntry(eb,reused);
            if(old_bytes!=reused || reused.capacity()!=reused.size()) return 3;
            std::cout << archive_entry_pathname(ea) << "," << old_bytes.size()
                      << "," << old_bytes.capacity() << "," << reused.capacity() << "\n";
            total_size+=reused.size(); old_capacity+=old_bytes.capacity();
            new_capacity+=reused.capacity(); ++count;
        }
        if(archive_read_next_header(b,&eb)!=ARCHIVE_EOF) return 4;
        archive_read_free(a); archive_read_free(b);
    }
    std::cout << "TOTAL," << count << "," << total_size << ","
              << old_capacity << "," << new_capacity << "\n";
}
'''
    cpp = args.output / "archive_capacity.cpp"
    cpp.write_text(source)
    executable = args.output / "archive_capacity"
    subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", str(cpp),
                    "-larchive", "-o", str(executable)], check=True)
    output = subprocess.check_output([str(executable), str(synthetic), str(streaming),
                                      *(str(path) for path in args.archive)], text=True)
    (args.output / "archive-capacity.csv").write_text(output)
    print(json.dumps({"source_commit": COMMIT, "result": output.splitlines()[-1],
                      "report": str(args.output / "archive-capacity.csv")}))


if __name__ == "__main__":
    main()
