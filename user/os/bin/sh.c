/* sh -- the PigeonOS shell (docs/shell.md).
 *
 * Shows a prompt, reads a line, splits it into words, and runs the program
 * the first word names, waiting for it. cd, exit and help are its own. The
 * kernel's console does the typing and the drawing; this only works out
 * what a line means.
 *
 * The prompt is /etc/shell_header.conf when there is one (shell.md §2). Its
 * first line is the prompt, where \n is a line break, \\ a backslash,
 * ``CWD`` the current directory, ``STATUS`` the last program's status,
 * ``CSTATUS`` the same colored by its sign, a color name its color and
 * ``RESET`` the normal ink. A second line, when
 * there is one, is shown once in its place: the first prompt after the shell
 * starts. Without the file the prompt is the current directory and "> ".
 *
 * A name with no '/' is /bin/<name>.bin, then <name>.bin here; a name with
 * one is a path. The .bin is added when it is missing (docs/kernel_exec.md
 * Q4). exit ends the shell, and the kernel starts it again, which reads the
 * prompt file again too.
 */
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define LINE        256
#define WORDS       16
#define PATH        264
#define PROMPT_FILE "/etc/shell_header.conf"
#define PROMPT_MAX  255u            /* bytes in each line of it */
#define FILE_MAX    1024u
#define BUILT_IN    "``RED````CWD``> ``RESET``"

static char prompt[256];            /* line 1: the prompt */
static char first[256];             /* line 2: the first prompt, or empty */
static int last_status;

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

/* One line of the prompt file into out, without a '\r' an editor left
 * before its line break or quotes around it: 0 if it is too long. */
static int take_line(char *line, char *out) {
    unsigned n = strlen(line);
    if (n > 0u && line[n - 1u] == '\r') n--;
    if (n > PROMPT_MAX) return 0;
    if (n >= 2u && line[0] == '"' && line[n - 1u] == '"') {
        line++;
        n = n - 2u;
    }
    strlcpy(out, line, n + 1u);
    return 1;
}

/* The prompt file into prompt[] and first[], without line breaks at its
 * end -- or the built-in prompt, when there is no file or it is refused. */
static void load_prompt(void) {
    char text[FILE_MAX + 1u];
    char more[1];
    char *second;
    char *why = NULL;
    int fd;
    int n;
    strlcpy(prompt, BUILT_IN, PROMPT_MAX + 1u);
    first[0] = 0;
    fd = open(PROMPT_FILE, O_READ);
    if (fd < 0) return;
    n = read(fd, text, FILE_MAX);
    if (n >= 0 && read(fd, more, 1u) > 0) why = "is over 1024 bytes";
    close(fd);
    if (n < 0) return;
    while (n > 0 && (text[n - 1] == '\n' || text[n - 1] == '\r')) n--;
    text[n] = 0;
    second = strchr(text, '\n');
    if (second != NULL) {
        *second = 0;
        second++;
        if (why == NULL && strchr(second, '\n') != NULL) why = "has more than 2 lines";
    }
    if (why == NULL && !take_line(text, prompt)) why = "has a line over 255 bytes";
    if (why == NULL && second != NULL && !take_line(second, first)) why = "has a line over 255 bytes";
    if (why != NULL) {
        printf("sh: %s %s\n", PROMPT_FILE, why);
        strlcpy(prompt, BUILT_IN, PROMPT_MAX + 1u);
        first[0] = 0;
    }
}

/* The console's escape code for a color name, or NULL. GREY is the
 * console's own ink, which is a light grey. */
static char *color_code(char *name) {
    if (strcmp(name, "BLACK") == 0) return "\x1b[30m";
    if (strcmp(name, "RED") == 0) return "\x1b[31m";
    if (strcmp(name, "GREEN") == 0) return "\x1b[32m";
    if (strcmp(name, "YELLOW") == 0) return "\x1b[33m";
    if (strcmp(name, "BLUE") == 0) return "\x1b[34m";
    if (strcmp(name, "MAGENTA") == 0) return "\x1b[35m";
    if (strcmp(name, "CYAN") == 0) return "\x1b[36m";
    if (strcmp(name, "WHITE") == 0) return "\x1b[37m";
    if (strcmp(name, "GREY") == 0) return "\x1b[39m";
    if (strcmp(name, "RESET") == 0) return "\x1b[0m";
    return NULL;
}

static void show_prompt(char *text) {
    char name[16];
    char cwd[PATH];
    char *code;
    unsigned i = 0u;
    unsigned j;
    unsigned k;
    print("\x1b]133;A\x07");               /* where the prompt starts, for Ctrl+L */
    while (text[i] != 0) {
        if (text[i] == '\\' && text[i + 1u] == 'n') {
            print("\n");
            i = i + 2u;
            continue;
        }
        if (text[i] == '\\' && text[i + 1u] == '\\') {
            print("\\");
            i = i + 2u;
            continue;
        }
        if (text[i] == '`' && text[i + 1u] == '`') {
            j = i + 2u;
            while (text[j] != 0 && !(text[j] == '`' && text[j + 1u] == '`') && j - i < 17u) j++;
            if (text[j] == '`' && text[j + 1u] == '`') {
                for (k = 0u; k < j - i - 2u; k++) name[k] = text[i + 2u + k];
                name[k] = 0;
                code = color_code(name);
                if (strcmp(name, "CWD") == 0) {
                    if (getcwd(cwd, PATH) < 0) strlcpy(cwd, "?", PATH);
                    print(cwd);
                    i = j + 2u;
                    continue;
                }
                if (strcmp(name, "STATUS") == 0) {
                    printf("%d", last_status);
                    i = j + 2u;
                    continue;
                }
                if (strcmp(name, "CSTATUS") == 0) {
                    /* white for 0, magenta for a program's exit value, red
                     * for an error: a crash, Ctrl+C, a file that isn't a
                     * program. The color stays on after it. */
                    if (last_status == 0) code = color_code("WHITE");
                    else if (last_status > 0) code = color_code("MAGENTA");
                    else code = color_code("RED");
                    printf("%s%d", code, last_status);
                    i = j + 2u;
                    continue;
                }
                if (code != NULL) {
                    print(code);
                    i = j + 2u;
                    continue;
                }
            }
        }
        write(STDOUT, text + i, 1u);        /* anything else, as written */
        i++;
    }
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
    char *shown;
    int n;
    int count;
    int status;

    setcomplete("/bin", "cd exit help");   /* for Tab: find() looks in /bin first */
    load_prompt();
    shown = prompt;
    if (first[0] != 0) shown = first;       /* line 2, once */
    while (1) {
        show_prompt(shown);
        shown = prompt;
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
            last_status = status < 0 ? 1 : 0;
            if (status < 0) printf("cd: %s\n", sys_strerror(status));
            continue;
        }

        if (!find(words[0], path)) {
            printf("%s: not found\n", words[0]);
            last_status = 1;
            continue;
        }
        status = exec(path, count, words);
        last_status = status;
        if (status < 0) printf("%s: %s\n", words[0], sys_strerror(status));
        else if (status > 0) printf("%s: exit %d\n", words[0], status);
    }
    return 0;
}
