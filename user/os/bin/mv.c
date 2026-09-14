/* mv -- move or rename a file or directory: mv FROM TO.
 * When TO is a directory, FROM goes inside it under its own name. Both must
 * be on the same disk. */
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define PATH 264u

/* TO, or TO/<FROM's last name> when TO is a directory. */
static void target(char *from, char *to, char *out) {
    sys_stat_t st;
    char *name = strrchr(from, '/');
    name = (name == NULL) ? from : name + 1;
    if (stat(to, &st) >= 0 && st.type == S_DIR) {
        if (to[strlen(to) - 1u] == '/') snprintf(out, PATH, "%s%s", to, name);
        else snprintf(out, PATH, "%s/%s", to, name);
    } else {
        strlcpy(out, to, PATH);
    }
}

int main(int argc, char **argv) {
    char to[PATH];
    int r;
    if (argc != 3) {
        printf("usage: mv FROM TO\n");
        return 1;
    }
    target(argv[1], argv[2], to);
    r = rename(argv[1], to);
    if (r < 0) {
        printf("mv: %s: %s\n", argv[1], sys_strerror(r));
        return 1;
    }
    return 0;
}
