/* rm -- remove each file named. A directory is rmdir's. */
#include <pigeon/stdio.h>
#include <pigeon/sys.h>

int main(int argc, char **argv) {
    int i;
    int r;
    int failed = 0;
    if (argc < 2) {
        printf("usage: rm FILE...\n");
        return 1;
    }
    for (i = 1; i < argc; i++) {
        r = remove(argv[i]);
        if (r < 0) {
            printf("rm: %s: %s\n", argv[i], sys_strerror(r));
            failed = 1;
        }
    }
    return failed;
}
