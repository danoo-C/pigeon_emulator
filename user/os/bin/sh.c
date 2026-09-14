/* sh -- the PigeonOS shell, as simple as it can be (docs/shell.md).
 *
 * Shows the current directory as the prompt, reads a line, splits it into
 * words, and runs the program the first word names, waiting for it. cd,
 * exit and help are its own. The kernel's console does the typing and the
 * drawing; this only works out what a line means.
 *
 * A name with no '/' is /bin/<name>.bin, then <name>.bin here; a name with
 * one is a path. The .bin is added when it is missing (docs/kernel_exec.md
 * Q4). exit ends the shell, and the kernel starts it again.
 */
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define LINE  256
#define WORDS 16
#define PATH  264

static void say(char *a, char *b, char *c) {
    print(a);
    print(b);
    print(c);
    print("\n");
}

/* Split a line in place at spaces; double quotes keep spaces in a word.
 * The count, or -1 for more than WORDS words. */
static int split(char *line, char **argv) {
    int argc = 0;
    char *p = line;
    char *out;
    char c;
    while (1) {
        while (*p == ' ') p++;
        if (*p == 0 || *p == '\n') break;
        if (argc == WORDS) return -1;
        argv[argc] = p;
        argc++;
        out = p;
        while (*p != 0 && *p != '\n' && *p != ' ') {
            if (*p == '"') {
                p++;
                while (*p != 0 && *p != '\n' && *p != '"') {
                    *out = *p;
                    out++;
                    p++;
                }
                if (*p == '"') p++;
            } else {
                *out = *p;
                out++;
                p++;
            }
        }
        c = *p;
        *out = 0;           /* out never passes p, so this ends the word in place */
        if (c == 0 || c == '\n') break;
        p++;
    }
    argv[argc] = (char *)0;
    return argc;
}

static int is_file(char *path) {
    sys_stat_t st;
    return stat(path, &st) >= 0 && st.type == S_FILE;
}

/* Where the program `name` is, into path: 1 if found. */
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

static void help(void) {
    print("cd DIR   go to a directory\n");
    print("exit     end the shell\n");
    print("help     this\n");
    print("Anything else runs a program;\n");
    print("ls /bin shows them.\n");
}

int main(int argc, char **argv) {
    char line[LINE];
    char *words[WORDS + 1];
    char path[PATH];
    char cwd[PATH];
    char number[12];
    int n;
    int count;
    int status;

    while (1) {
        if (getcwd(cwd, PATH) < 0) strlcpy(cwd, "?", PATH);
        print(cwd);
        print("> ");
        n = read(STDIN, line, LINE - 1u);
        if (n <= 0) continue;
        line[n] = 0;
        count = split(line, words);
        if (count < 0) {
            print("too many words\n");
            continue;
        }
        if (count == 0) continue;

        if (strcmp(words[0], "exit") == 0) return 0;
        if (strcmp(words[0], "help") == 0) {
            help();
            continue;
        }
        if (strcmp(words[0], "cd") == 0) {
            status = chdir(count > 1 ? words[1] : "/");
            if (status < 0) say("cd: ", sys_strerror(status), "");
            continue;
        }

        if (!find(words[0], path)) {
            say(words[0], ": ", "not found");
            continue;
        }
        status = exec(path, count, words);
        if (status < 0) {
            say(words[0], ": ", sys_strerror(status));
        } else if (status > 0) {
            itoa(status, number);
            say(words[0], ": exit ", number);
        }
    }
    return 0;
}
