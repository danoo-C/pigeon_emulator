/* ls -- list a directory: the current one, or each one named.
 * A directory's name ends in '/'. */
#include <pigeon/sys.h>

static int list(char *dir) {
    sys_stat_t st;
    int dh = opendir(dir);
    if (dh < 0) {
        print("ls: ");
        print(dir);
        print(": ");
        print(sys_strerror(dh));
        print("\n");
        return 1;
    }
    while (readdir(dh, &st) == 1) {
        print(st.name);
        if (st.type == S_DIR) print("/");
        print("\n");
    }
    closedir(dh);
    return 0;
}

int main(int argc, char **argv) {
    int i;
    int failed = 0;
    if (argc < 2) return list(".");
    for (i = 1; i < argc; i++) {
        if (argc > 2) {
            print(argv[i]);
            print(":\n");
        }
        failed = failed | list(argv[i]);
    }
    return failed;
}
