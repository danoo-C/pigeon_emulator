/* pgs -- the pigeon shell's scripts (docs/pgs_plan.md).
 *
 *     pgs FILE [ARGS...]
 *
 * A script is lines. A line is a command, run the way the shell runs one; or
 * an assignment, `$name = value`; or a `#` setting; or blank. `;;` starts a
 * comment, anywhere outside quotes.
 *
 *     ;; what this script does
 *     # stop-on-error                a program that fails ends the script
 *
 *     $name = world                  a value: the rest of the line, expanded
 *     $files = $(ls /bin)            what a program printed, instead
 *     echo hello $name               a command, its words expanded
 *
 * Words are split at spaces, double quotes grouping them, and each word is
 * expanded on its own afterwards -- so a value with spaces in it stays one
 * argument, always. `\$` is a dollar, `\\` a backslash and `\"` a quote.
 *
 * $0 is the script, $1 to $9 its arguments, $# how many there are, and $?
 * the last command's status. A name that was never set stops the script:
 * there is no way to turn that on here, and a typo that quietly becomes an
 * empty string is the most expensive bug a small language can have.
 *
 * $( ) runs a command with the kernel's exec_out and hands back what it
 * printed, with the newlines at the end taken off. The buffer is 8 KB; past
 * that the output is cut and the script is told so. Only STDOUT is taken,
 * so a program's complaints still reach the screen, and this script's own
 * complaints go to STDERR for the same reason: a script whose output is
 * being captured can still say what went wrong.
 *
 * Everything that isn't a builtin is a program, found as the shell finds
 * one: /bin/<name>.bin first, then <name>.bin where you are, with the .bin
 * added when it is missing. A program that fails doesn't stop the script --
 * that is what $? is for -- unless the script said `# stop-on-error`.
 *
 * Values live on the heap, one block each, at most 8 KB: the same size as a
 * capture, so a listing can be kept in a variable. Setting one frees the
 * block it had, and mem.c's first fit hands the same block back when the new
 * value fits it, which is what a loop assigning the same variable does.
 */
#include <pigeon/mem.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define SCRIPT_MAX  16384u          /* bytes of script                     */
#define LINES_MAX   1024
#define VARS        32
#define NAME_MAX    32u             /* 31 and a terminator                 */
#define VALUE_MAX   8192u           /* as big as a capture                 */
#define CAPTURE     8192u
#define WORDS       16              /* words in a command                  */
#define STORE       16384u          /* all the words of one line, packed   */
#define INNER_STORE 2048u           /* and of the command inside a $( )    */
#define PATH        264

static char text[SCRIPT_MAX + 1u];  /* the script, read whole              */
static char *lines[LINES_MAX];
static int n_lines;

static char *script;                /* its path, as it was given to us     */
static unsigned at;                 /* the line being run, from 1          */
static int running;
static int status_out;              /* what pgs itself returns             */
static int last_status;             /* $?                                  */
static int started;                 /* a command has been run: no more settings */
static int stop_on_error;           /* # stop-on-error                     */

static char **args;                 /* $0 is the script, $1.. its arguments */
static int n_args;

static char var_name[VARS * NAME_MAX];
static char *var_value[VARS];       /* on the heap, freed when set again   */
static int n_vars;

static char store[STORE];           /* the words of the line being run     */
static char *words[WORDS + 1];
static char inner_store[INNER_STORE];   /* and of the one inside a $( )    */
static char *inner_words[WORDS + 1];
static char cap_buf[CAPTURE];       /* what a $( ) caught                  */
static int capturing;               /* 1 while a builtin's output is taken */
static char *sink;                  /* where it goes, and how much room    */
static char *sink_end;

static char value_buf[VALUE_MAX];   /* an assignment's value, expanded     */

/* --- saying what went wrong ----------------------------------------------
 *
 * To STDERR, which a capture never takes, so a script being captured can
 * still complain where it can be read. */

static void say(char *s) {
    write(STDERR, s, strlen(s));
}

/* "hello.pgs:7: no such variable: $nmae", and the script stops. */
static void fail(char *what, char *detail) {
    char line[512];
    snprintf(line, sizeof(line), "%s:%u: %s%s\n", script, at, what, detail);
    say(line);
    running = 0;
    status_out = 1;
}

/* The same, without stopping: something the script should know about. */
static void warn(char *what, char *detail) {
    char line[512];
    snprintf(line, sizeof(line), "%s:%u: %s%s\n", script, at, what, detail);
    say(line);
}

/* --- text ----------------------------------------------------------------- */

static char *trim(char *s) {
    unsigned n;
    while (*s == ' ' || *s == '\t') s++;
    n = strlen(s);
    while (n > 0u && (s[n - 1u] == ' ' || s[n - 1u] == '\t' || s[n - 1u] == '\r')) n--;
    s[n] = 0;
    return s;
}

static int is_name_char(int c) {
    return (isalpha(c) != 0 || isdigit(c) != 0 || c == '_') ? 1 : 0;
}

/* --- the output a builtin makes ------------------------------------------
 * Straight to STDOUT, or into a $( )'s buffer when one is taking it. */

static void out(char *s) {
    unsigned n = strlen(s);
    unsigned i;
    if (capturing == 0) {
        write(STDOUT, s, n);
        return;
    }
    for (i = 0u; i < n; i++) {
        if (sink >= sink_end) return;
        *sink = s[i];
        sink++;
        *sink = 0;
    }
}

/* --- variables -------------------------------------------------------------- */

static int var_index(char *name) {
    int i;
    for (i = 0; i < n_vars; i++) {
        if (strcmp(var_name + (unsigned)i * NAME_MAX, name) == 0) return i;
    }
    return -1;
}

static char *var_get(char *name) {
    int i = var_index(name);
    return (i < 0) ? NULL : var_value[i];
}

static int var_set(char *name, char *value) {
    unsigned n = strlen(value);
    char *copy;
    int i;

    if (n >= VALUE_MAX) {
        fail("value too long for $", name);
        return 0;
    }
    copy = (char *)malloc(n + 1u);
    if (copy == NULL) {
        fail("no room for $", name);
        return 0;
    }
    strcpy(copy, value);
    i = var_index(name);
    if (i >= 0) {
        free(var_value[i]);             /* the same block comes back for the same size */
        var_value[i] = copy;
        return 1;
    }
    if (n_vars == VARS) {
        free(copy);
        fail("too many variables at $", name);
        return 0;
    }
    strlcpy(var_name + (unsigned)n_vars * NAME_MAX, name, NAME_MAX);
    var_value[n_vars] = copy;
    n_vars++;
    return 1;
}

/* --- finding a program, as the shell finds one (sh.c) ------------------------ */

static int is_file(char *path) {
    sys_stat_t st;
    return (stat(path, &st) >= 0 && st.type == S_FILE) ? 1 : 0;
}

static int find_program(char *name, char *path) {
    unsigned n = strlen(name);
    int bare = (n < 4u || strcmp(name + n - 4u, ".bin") != 0) ? 1 : 0;
    if (strchr(name, '/') == NULL) {
        strlcpy(path, "/bin/", PATH);
        strlcat(path, name, PATH);
        if (bare) strlcat(path, ".bin", PATH);
        if (is_file(path)) return 1;
    }
    strlcpy(path, name, PATH);
    if (bare) strlcat(path, ".bin", PATH);
    return is_file(path);
}

/* --- splitting and expanding -------------------------------------------------
 *
 * One pass: the words come apart at spaces while each one is expanded, so a
 * value with a space in it can never become two arguments. */

static int split_words(char *src, char **out_words, char *out_store, unsigned room,
                       int max, int inside);

static int append(char **oo, char *end, char *s) {
    while (*s != 0) {
        if (*oo >= end) {
            fail("the line is too long", "");
            return 0;
        }
        **oo = *s;
        (*oo)++;
        s++;
    }
    return 1;
}

static int append_char(char **oo, char *end, char c) {
    char one[2];
    one[0] = c;
    one[1] = 0;
    return append(oo, end, one);
}

/* What a builtin is: run_builtin knows these and nothing else does. */
static int is_builtin(char *word) {
    if (strcmp(word, "echo") == 0) return 1;
    if (strcmp(word, "cd") == 0) return 1;
    if (strcmp(word, "pwd") == 0) return 1;
    if (strcmp(word, "exit") == 0) return 1;
    return 0;
}

static void run_builtin(int count, char **w);

/* `command` run with its output taken: a program through the kernel's
 * exec_out, a builtin through the sink. What it printed lands in cap_buf,
 * without the newlines at its end. */
static int capture(char *command, char **oo, char *end) {
    char path[PATH];
    int count;
    unsigned n;

    cap_buf[0] = 0;
    count = split_words(command, inner_words, inner_store, INNER_STORE, WORDS, 1);
    if (count < 0) return 0;
    if (count == 0) {
        fail("nothing to run in $( )", "");
        return 0;
    }
    if (is_builtin(inner_words[0])) {
        capturing = 1;
        sink = cap_buf;
        sink_end = cap_buf + CAPTURE - 1u;
        run_builtin(count, inner_words);
        capturing = 0;
    } else if (!find_program(inner_words[0], path)) {
        warn("not found: ", inner_words[0]);
        last_status = E_NOENT;
        return 1;                       /* an empty value, and $? says why */
    } else {
        last_status = exec_out(path, count, inner_words, cap_buf, CAPTURE);
    }
    n = strlen(cap_buf);
    if (n == CAPTURE - 1u) warn("$( ) cut at 8191 bytes: ", inner_words[0]);
    while (n > 0u && (cap_buf[n - 1u] == '\n' || cap_buf[n - 1u] == '\r')) {
        n--;
        cap_buf[n] = 0;
    }
    return append(oo, end, cap_buf);
}

/* One $... at *pp, its value appended at *oo. 0 when the script must stop. */
static int take_dollar(char **pp, char **oo, char *end, int inside) {
    char name[NAME_MAX];
    char number[STR_UTOA_MAX];
    char command[INNER_STORE];
    char *p = *pp + 1;                  /* past the '$' */
    char *value;
    unsigned n;
    int depth;

    if (*p == '?') {
        itoa(last_status, number);
        *pp = p + 1;
        return append(oo, end, number);
    }
    if (*p == '#') {
        itoa(n_args, number);
        *pp = p + 1;
        return append(oo, end, number);
    }
    if (isdigit((int)*p) != 0 && is_name_char((int)p[1]) == 0) {
        n = (unsigned)(*p - '0');
        *pp = p + 1;
        if ((int)n > n_args) return append(oo, end, "");
        return append(oo, end, args[n]);
    }
    if (*p == '(') {
        if (inside != 0) {
            fail("$( ) inside $( )", "");
            return 0;
        }
        p++;
        depth = 0;
        n = 0u;
        while (*p != 0 && !(*p == ')' && depth == 0)) {
            if (*p == '(') depth++;
            if (*p == ')') depth--;
            if (n + 1u >= INNER_STORE) {
                fail("the command in $( ) is too long", "");
                return 0;
            }
            command[n] = *p;
            n++;
            p++;
        }
        if (*p != ')') {
            fail("no ) for $(", "");
            return 0;
        }
        command[n] = 0;
        *pp = p + 1;
        return capture(command, oo, end);
    }
    if (is_name_char((int)*p) == 0) {   /* a lone $ is a dollar */
        *pp = p;
        return append_char(oo, end, '$');
    }
    n = 0u;
    while (is_name_char((int)p[n]) != 0) {
        if (n + 1u >= NAME_MAX) {
            fail("the name after $ is too long", "");
            return 0;
        }
        name[n] = p[n];
        n++;
    }
    name[n] = 0;
    *pp = p + n;
    value = var_get(name);
    if (value == NULL) {
        fail("no such variable: $", name);
        return 0;
    }
    return append(oo, end, value);
}

/* The words of `src`, expanded, into out_words. Their text is packed into
 * out_store. -1 when the script must stop. `inside` says we are already in a
 * $( ), where another one is refused. */
static int split_words(char *src, char **out_words, char *out_store, unsigned room,
                       int max, int inside) {
    char *p = src;
    char *o = out_store;
    char *end = out_store + room - 1u;
    int count = 0;
    int quoted;

    while (*p != 0) {
        while (*p == ' ' || *p == '\t') p++;
        if (*p == 0) break;
        if (p[0] == ';' && p[1] == ';') break;          /* a comment ends the line */
        if (count == max) {
            fail("too many words", "");
            return -1;
        }
        out_words[count] = o;
        count++;
        quoted = 0;
        while (*p != 0) {
            if (quoted == 0 && (*p == ' ' || *p == '\t')) break;
            if (quoted == 0 && p[0] == ';' && p[1] == ';') break;
            if (*p == '"') {
                quoted = (quoted == 0) ? 1 : 0;
                p++;
                continue;
            }
            if (*p == '\\' && (p[1] == '$' || p[1] == '\\' || p[1] == '"')) {
                if (!append_char(&o, end, p[1])) return -1;
                p = p + 2;
                continue;
            }
            if (*p == '$') {
                if (!take_dollar(&p, &o, end, inside)) return -1;
                continue;
            }
            if (!append_char(&o, end, *p)) return -1;
            p++;
        }
        if (quoted != 0) {
            fail("a \" with no \" after it", "");
            return -1;
        }
        if (o >= end) {
            fail("the line is too long", "");
            return -1;
        }
        *o = 0;
        o++;
    }
    out_words[count] = NULL;
    return count;
}

/* The whole of `src` expanded into one value, spaces and all: the right-hand
 * side of an assignment. 0 when the script must stop. */
static int expand_value(char *src, char *dst, unsigned room) {
    char *p = src;
    char *o = dst;
    char *end = dst + room - 1u;
    int quoted = 0;

    while (*p != 0) {
        if (quoted == 0 && p[0] == ';' && p[1] == ';') break;
        if (*p == '"') {
            quoted = (quoted == 0) ? 1 : 0;
            p++;
            continue;
        }
        if (*p == '\\' && (p[1] == '$' || p[1] == '\\' || p[1] == '"')) {
            if (!append_char(&o, end, p[1])) return 0;
            p = p + 2;
            continue;
        }
        if (*p == '$') {
            if (!take_dollar(&p, &o, end, 0)) return 0;
            continue;
        }
        if (!append_char(&o, end, *p)) return 0;
        p++;
    }
    if (quoted != 0) {
        fail("a \" with no \" after it", "");
        return 0;
    }
    *o = 0;
    while (o > dst && (o[-1] == ' ' || o[-1] == '\t')) {    /* the spaces before a comment */
        o--;
        *o = 0;
    }
    return 1;
}

/* --- the builtins -------------------------------------------------------------- */

static void run_builtin(int count, char **w) {
    char cwd[PATH];
    int status;
    int i;

    if (strcmp(w[0], "echo") == 0) {
        for (i = 1; i < count; i++) {
            out(w[i]);
            if (i + 1 < count) out(" ");
        }
        out("\n");
        last_status = 0;
        return;
    }
    if (strcmp(w[0], "pwd") == 0) {
        if (getcwd(cwd, PATH) < 0) {
            warn("pwd: ", "where are we?");
            last_status = 1;
            return;
        }
        out(cwd);
        out("\n");
        last_status = 0;
        return;
    }
    if (strcmp(w[0], "cd") == 0) {
        status = chdir((count > 1) ? w[1] : "/");
        if (status < 0) {
            warn((count > 1) ? w[1] : "/", "");
            last_status = 1;
            return;
        }
        last_status = 0;
        return;
    }
    /* exit */
    status_out = (count > 1) ? atoi(w[1]) : last_status;
    last_status = status_out;
    running = 0;
}

/* --- a line ---------------------------------------------------------------------- */

/* `# stop-on-error`: a setting, named to turn it on. They live in the header,
 * before the first command, so one is read before it could matter, and a word
 * pgs doesn't know is a mistake rather than a shrug. */
static void directive(char *line) {
    char *word;
    char *p;

    word = line + 1;
    for (p = word; *p != 0; p++) {              /* a comment after it */
        if (p[0] == ';' && p[1] == ';') {
            *p = 0;
            break;
        }
    }
    word = trim(word);
    if (started != 0) {
        fail("a setting after the first command: ", word);
        return;
    }
    if (strcmp(word, "stop-on-error") == 0) {
        stop_on_error = 1;
        return;
    }
    fail("no such setting: ", word);
}

/* `$name = value`: the first word starts with $ and the second is =. The
 * name is not expanded -- it is being set. */
static int assignment(char *line) {
    char name[NAME_MAX];
    char *p = line + 1;
    unsigned n = 0u;

    if (*line != '$') return 0;
    while (is_name_char((int)p[n]) != 0) {
        if (n + 1u >= NAME_MAX) return 0;
        name[n] = p[n];
        n++;
    }
    name[n] = 0;
    if (n == 0u) return 0;
    p = p + n;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '=') return 0;
    p++;
    while (*p == ' ' || *p == '\t') p++;

    started = 1;
    if (!expand_value(p, value_buf, VALUE_MAX)) return 1;
    var_set(name, value_buf);
    return 1;
}

static void run_command(char *line) {
    char path[PATH];
    int count;

    count = split_words(line, words, store, STORE, WORDS, 0);
    if (count <= 0) return;             /* nothing, or a mistake already said */
    started = 1;
    if (is_builtin(words[0])) {
        run_builtin(count, words);
    } else if (!find_program(words[0], path)) {
        warn(words[0], ": not found");
        last_status = E_NOENT;
    } else {
        last_status = exec(path, count, words);
    }
    if (last_status != 0 && stop_on_error != 0 && running != 0) {
        char number[STR_UTOA_MAX];
        itoa(last_status, number);
        fail("stopped: ", number);
        status_out = last_status;
    }
}

static void do_line(char *line) {
    char *p = trim(line);

    if (*p == 0) return;
    if (p[0] == ';' && p[1] == ';') return;
    if (*p == '#') {
        directive(p);
        return;
    }
    if (assignment(p)) return;
    run_command(p);
}

/* --- the script ------------------------------------------------------------------- */

static int load_script(char *path) {
    char more[1];
    int fd;
    int n;

    fd = open(path, O_READ);
    if (fd < 0) {
        say("pgs: ");
        say(path);
        say(": ");
        say(sys_strerror(fd));
        say("\n");
        return 0;
    }
    n = read(fd, text, SCRIPT_MAX);
    if (n >= 0 && read(fd, more, 1u) > 0) {
        close(fd);
        say("pgs: ");
        say(path);
        say(": over 16384 bytes\n");
        return 0;
    }
    close(fd);
    if (n < 0) {
        say("pgs: ");
        say(path);
        say(": ");
        say(sys_strerror(n));
        say("\n");
        return 0;
    }
    text[n] = 0;
    return 1;
}

/* The script's lines, cut in place. A last line with no newline counts. */
static int split_lines(void) {
    char *p = text;
    char *nl;

    n_lines = 0;
    while (1) {
        if (n_lines == LINES_MAX) {
            say("pgs: over 1024 lines\n");
            return 0;
        }
        lines[n_lines] = p;
        n_lines++;
        nl = strchr(p, '\n');
        if (nl == NULL) break;
        *nl = 0;
        p = nl + 1;
        if (*p == 0) break;
    }
    return 1;
}

int main(int argc, char **argv) {
    int i;

    if (argc < 2) {
        say("usage: pgs FILE [ARGS...]\n");
        return 1;
    }
    script = argv[1];
    args = argv + 1;                    /* $0 is the script, $1.. its arguments */
    n_args = argc - 2;
    if (!load_script(script)) return 1;
    if (!split_lines()) return 1;

    running = 1;
    for (i = 0; i < n_lines && running != 0; i++) {
        at = (unsigned)i + 1u;
        do_line(lines[i]);
    }
    return status_out;
}
