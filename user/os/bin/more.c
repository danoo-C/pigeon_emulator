/* more -- page files, or a command's output, a screen at a time
 * (docs/phase4b_plan.md step 6).
 *
 *     more FILE...          the files, one after another
 *     more COMMAND ARGS...  the command, run with its output paged
 *
 * The paging is the console's: after a screen of output it shows
 * -- more -- and waits. Space shows another screen, Enter one more row,
 * PgUp, PgDn and the wheel look back, and q stops -- the command, with
 * everything it ran, or the files. A first word that names a file means
 * files; otherwise it is found as the shell finds a program, /bin first.
 */
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define PATH  264
#define CHUNK 256u

static int is_file(char *path) {
    sys_stat_t st;
    return stat(path, &st) >= 0 && st.type == S_FILE;
}

/* Where the program `name` is, into path, as sh.c finds it: 1 if found. */
static int find(char *name, char *path) {
    unsigned n = strlen(name);
    int bare = n < 4u || strcmp(name + n - 4u, ".bin") != 0;
    if (strchr(name, '/') == (char *)0) {
        strlcpy(path, "/bin/", PATH);
        strlcat(path, name, PATH);
        if (bare) strlcat(path, ".bin", PATH);
        if (is_file(path)) return 1;
    }
    strlcpy(path, name, PATH);
    if (bare) strlcat(path, ".bin", PATH);
    return is_file(path);
}

/* A file to the console: 0, 1 if it couldn't be read, or -1 for q. */
static int page_file(char *path) {
    char buf[CHUNK];
    int fd;
    int n;
    fd = open(path, O_READ);
    if (fd < 0) {
        printf("more: %s: %s\n", path, sys_strerror(fd));
        return 1;
    }
    n = read(fd, buf, CHUNK);
    while (n > 0) {
        if (write(STDOUT, buf, (unsigned)n) == E_QUIT) {
            close(fd);
            return -1;
        }
        n = read(fd, buf, CHUNK);
    }
    close(fd);
    return n < 0 ? 1 : 0;
}

int main(int argc, char **argv) {
    char path[PATH];
    int i;
    int status;
    int failed = 0;
    if (argc < 2) {
        print("usage: more FILE... or more COMMAND ARGS...\n");
        return 1;
    }
    paging(1);
    if (is_file(argv[1])) {
        for (i = 1; i < argc; i++) {
            status = page_file(argv[i]);
            if (status < 0) return 0;
            failed = failed | status;
        }
        return failed;
    }
    if (!find(argv[1], path)) {
        printf("more: %s: not found\n", argv[1]);
        return 1;
    }
    status = exec(path, argc - 1, argv + 1);
    if (status == ENDED_QUIT) return 0;
    if (status < 0) {
        printf("more: %s: %s\n", argv[1], sys_strerror(status));
        return 1;
    }
    return status;
}
