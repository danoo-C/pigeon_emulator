/* cp -- copy a file: cp FROM TO.
 * When TO is a directory, the copy goes inside it under FROM's name. An
 * existing file is replaced. A copy that runs out of room says so and leaves
 * what it managed to write. */
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define PATH  264u
#define CHUNK 512u

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

static int fail(char *path, int r) {
    printf("cp: %s: %s\n", path, sys_strerror(r));
    return 1;
}

int main(int argc, char **argv) {
    char to[PATH];
    char buf[CHUNK];
    sys_stat_t st;
    int in;
    int out;
    int n;
    int wrote;
    int r;
    if (argc != 3) {
        printf("usage: cp FROM TO\n");
        return 1;
    }
    r = stat(argv[1], &st);
    if (r < 0) return fail(argv[1], r);
    if (st.type == S_DIR) return fail(argv[1], -4);            /* is a directory */
    target(argv[1], argv[2], to);

    in = open(argv[1], O_READ);
    if (in < 0) return fail(argv[1], in);
    out = open(to, O_WRITE | O_CREATE | O_TRUNC);
    if (out < 0) {
        close(in);
        return fail(to, out);
    }
    r = 0;
    n = read(in, buf, CHUNK);
    while (n > 0) {
        wrote = write(out, buf, (unsigned)n);
        if (wrote != n) {
            r = (wrote < 0) ? wrote : -6;                       /* disk full */
            break;
        }
        n = read(in, buf, CHUNK);
    }
    if (n < 0) r = n;
    close(out);
    close(in);
    if (r < 0) return fail(to, r);
    return 0;
}
