#include <errno.h>
#include <stdlib.h>
#include <stdint.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char executable_path[PATH_MAX];
    uint32_t size = sizeof(executable_path);
    if (_NSGetExecutablePath(executable_path, &size) != 0) {
        fprintf(stderr, "DAClaw launcher path is too long.\n");
        return 1;
    }

    char resolved_path[PATH_MAX];
    if (realpath(executable_path, resolved_path) == NULL) {
        fprintf(stderr, "DAClaw launcher realpath failed: %s\n", strerror(errno));
        return 1;
    }

    char path_copy[PATH_MAX];
    strncpy(path_copy, resolved_path, sizeof(path_copy) - 1);
    path_copy[sizeof(path_copy) - 1] = '\0';

    char *macos_dir = dirname(path_copy);
    char script_path[PATH_MAX];
    int written = snprintf(
        script_path,
        sizeof(script_path),
        "%s/../Resources/launch-daclaw.sh",
        macos_dir
    );
    if (written < 0 || written >= (int)sizeof(script_path)) {
        fprintf(stderr, "DAClaw script path is too long.\n");
        return 1;
    }

    char **child_argv = calloc((size_t)argc + 3, sizeof(char *));
    if (child_argv == NULL) {
        fprintf(stderr, "DAClaw launcher allocation failed.\n");
        return 1;
    }

    child_argv[0] = "/bin/bash";
    child_argv[1] = script_path;
    for (int i = 1; i < argc; i++) {
        child_argv[i + 1] = argv[i];
    }
    child_argv[argc + 1] = NULL;

    execv("/bin/bash", child_argv);
    fprintf(stderr, "DAClaw launcher exec failed: %s\n", strerror(errno));
    return 1;
}
