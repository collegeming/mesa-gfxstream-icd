// Same-process semantics for the AHB allocation file switch.
//
// Production only re-checks the file; nothing caches the decision. This test
// covers that contract directly: absent -> present -> absent flips IsOff() with
// no intervening state, and GFXSTREAM_AHB_ALLOC_OFF_FILE relocates the path
// without enabling the switch by itself.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <string>

#include "ahb_alloc_off_file.h"

namespace {

void check(bool condition, const char* what) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", what);
        exit(1);
    }
}

void makeFile(const char* path) {
    FILE* file = fopen(path, "w");
    check(file != nullptr, "create off file");
    fclose(file);
}

}  // namespace

int main() {
    char directoryTemplate[] = "/tmp/gfxstream-ahb-off-XXXXXX";
    char* directory = mkdtemp(directoryTemplate);
    check(directory != nullptr, "mkdtemp");

    std::string path = std::string(directory) + "/switch";
    unlink(path.c_str());

    // Failure and non-file inputs are "not disabled", never an error.
    check(!gfxstream::ahb_alloc_killswitch::IsOff(nullptr), "null path is not off");
    check(!gfxstream::ahb_alloc_killswitch::IsOff(""), "empty path is not off");
    check(!gfxstream::ahb_alloc_killswitch::IsOff(path.c_str()), "absent file is not off");
    check(!gfxstream::ahb_alloc_killswitch::IsOff(directory), "a directory is not off");

    // Same process, no restart: create flips it on, unlink flips it off again.
    makeFile(path.c_str());
    check(gfxstream::ahb_alloc_killswitch::IsOff(path.c_str()),
          "existing regular file is off");
    unlink(path.c_str());
    check(!gfxstream::ahb_alloc_killswitch::IsOff(path.c_str()), "removed file is not off");

    // The environment only relocates the path; the file still decides.
    check(setenv("GFXSTREAM_AHB_ALLOC_OFF_FILE", path.c_str(), 1) == 0, "setenv");
    check(strcmp(gfxstream::ahb_alloc_killswitch::OffFilePath(), path.c_str()) == 0,
          "env override is the path");
    check(!gfxstream::ahb_alloc_killswitch::IsOff(
              gfxstream::ahb_alloc_killswitch::OffFilePath()),
          "env alone must not disable allocation");
    makeFile(path.c_str());
    check(gfxstream::ahb_alloc_killswitch::IsOff(
              gfxstream::ahb_alloc_killswitch::OffFilePath()),
          "file at the env path disables allocation");
    unlink(path.c_str());
    check(!gfxstream::ahb_alloc_killswitch::IsOff(
              gfxstream::ahb_alloc_killswitch::OffFilePath()),
          "removal takes effect immediately in the same process");

    rmdir(directory);
    puts("PASS: stat()-based AHB kill switch flips on file creation and removal in one "
         "process; failures and non-regular paths are not disabled; the env var only "
         "relocates the path");
    return 0;
}
