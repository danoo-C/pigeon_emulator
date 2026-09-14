/* rmdir -- remove each directory named, which must be empty. */
#include <pigeon/stdio.h>
#include <pigeon/sys.h>

int main(int argc, char **argv) {
    int i;
    int r;
    int failed = 0;
    if (argc < 2) {
        printf("usage: rmdir DIR...\n");
        return 1;
    }
    for (i = 1; i < argc; i++) {
        r = rmdir(argv[i]);
        if (r < 0) {
            printf("rmdir: %s: %s\n", argv[i], sys_strerror(r));
            failed = 1;
        }
    }
    return failed;
}
