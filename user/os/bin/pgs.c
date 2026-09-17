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
 *     # graphics                     the script owns the screen: black, kept
 *                                    between commands, and nothing but
 *                                    graphics.bin paints on it
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
 * if/else/end, while/end and for/end make the shapes; `let` does whole-number
 * sums, `break` leaves the innermost loop, and `read $name` takes a typed
 * line. echo, cd, pwd and exit are the builtins.
 *
 * `ls > out.txt`, `>>` and `< in.txt` work as they do at the prompt, on a
 * builtin as well as a program (docs/redirect_plan.md).
 *
 * Everything else is a program, found as the shell finds
 * one: /bin/<name>.bin first, then <name>.bin where you are, with the .bin
 * added when it is missing. A program that fails doesn't stop the script --
 * that is what $? is for -- unless the script said `# stop-on-error`.
 *
 * A line that ends in `\` is joined to the next one -- a drawing call with
 * a shape to a line is what it was added for. Inside quotes a backslash is
 * text, and the line numbers in messages stay the script's own.
 *
 * Values live on the heap, one block each, at most 8 KB: the same size as a
 * capture, so a listing can be kept in a variable. Setting one frees the
 * block it had, and mem.c's first fit hands the same block back when the new
 * value fits it, which is what a loop assigning the same variable does.
 */
#include <pigeon/display.h>
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
#define WORDS       32              /* words in a command: a graphics call
                                     * is six of them a shape             */
#define STORE       16384u          /* all the words of one line, packed   */
#define INNER_STORE 2048u           /* and of the command inside a $( )    */
#define PATH        264

static char text[SCRIPT_MAX + 1u];  /* the script, read whole              */
static char *lines[LINES_MAX];
static unsigned line_no[LINES_MAX]; /* the script's own numbering, which a
                                     * continued line does not change      */
static int n_lines;

static char *script;                /* its path, as it was given to us     */
static unsigned at;                 /* the line being run, as the script
                                     * numbers it: what a message says     */
static int at_index;                /* and which of lines[] that is        */
static int running;
static int status_out;              /* what pgs itself returns             */
static int last_status;             /* $?                                  */
static int started;                 /* a command has been run: no more settings */
static int stop_on_error;           /* # stop-on-error                     */
static int graphics_mode;           /* # graphics                          */
static int screen_kept;             /* and the screen is ours already      */
static char gfx_bin[64];            /* where a command's output goes then  */

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
static int out_fd;                  /* a builtin's output into a file, or -1 */

static char was_quoted[WORDS + 1];  /* the word had quotes: ">" is text      */
static char *redirect_out;          /* > or >> FILE on this line, or NULL    */
static char *redirect_in;           /* < FILE                                */
static unsigned redirect_how;

static char value_buf[VALUE_MAX];   /* an assignment's value, expanded     */

/* Blocks: if/else/end, while/end and for/end, innermost last. A line runs
 * when every block it is inside is running, so `break` is one flag and the
 * lines after it are skipped without looking at them. */
#define BLOCKS  8
#define B_IF    1
#define B_WHILE 2
#define B_FOR   3

static int b_kind[BLOCKS];
static int b_run[BLOCKS];           /* 1 while this block's body runs      */
static int b_taken[BLOCKS];         /* an if whose branch ran: else is not */
static int b_line[BLOCKS];          /* the line it started on              */
static char b_name[BLOCKS * NAME_MAX];  /* a for's variable                */
static char *b_text[BLOCKS];        /* a for's value, on the heap          */
static unsigned b_pos[BLOCKS];      /* how far through it we are           */
static int n_blocks;
static int jump_to;                 /* the line to carry on from, or -1    */
static int in_test;                 /* a command being run as a test       */

/* --- saying what went wrong ----------------------------------------------
 *
 * To STDERR, which a capture never takes, so a script being captured can
 * still complain where it can be read. */

static void say(char *s) {
    write(STDERR, s, strlen(s));
}

/* `# graphics`: the screen the script owns. Black, and kept between the
 * programs it runs -- the kernel paints its console back over the picture
 * after each one otherwise (docs/graphics_plan.md 4.3). Once, before the
 * first command, so a header with a mistake in it does not blank the
 * screen on its way out. */
static void graphics_start(void) {
    if (graphics_mode == 0 || screen_kept != 0) return;
    screen_kept = 1;
    keepscreen(1);
    disp_clear(BLACK);
}

/* And off again, for a message that has to be read. The console comes back
 * when pgs ends, whatever happens, since the kernel clears the screen with
 * the program that kept it. */
static void graphics_off(void) {
    if (graphics_mode == 0) return;
    graphics_mode = 0;
    if (screen_kept != 0) keepscreen(0);
}

/* "hello.pgs:7: no such variable: $nmae", and the script stops. */
static void fail(char *what, char *detail) {
    char line[512];
    graphics_off();                 /* a message is read on a console */
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
        /* Nothing paints on a graphics script's screen: an echo goes where
         * a program's output goes, which is nowhere, and is not a mistake
         * -- a script gets run both ways while it is being written. A
         * redirection still writes its file. */
        if (graphics_mode != 0 && out_fd < 0) return;
        /* A builtin's own words: to the file when the line redirects, since
         * `echo hello > note.txt` is a large part of why a script wants > at
         * all (docs/redirect_plan.md 3.3). */
        write((out_fd >= 0) ? out_fd : STDOUT, s, n);
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
        if (count <= WORDS) was_quoted[count] = 0;
        count++;
        quoted = 0;
        while (*p != 0) {
            if (quoted == 0 && (*p == ' ' || *p == '\t')) break;
            if (quoted == 0 && p[0] == ';' && p[1] == ';') break;
            if (*p == '"') {
                quoted = (quoted == 0) ? 1 : 0;
                if (count <= WORDS) was_quoted[count - 1] = 1;
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

/* --- blocks ---------------------------------------------------------------------- */

/* A line runs only when every block around it is running. */
static int live_below(int n) {
    int i;
    for (i = 0; i < n; i++) {
        if (b_run[i] == 0) return 0;
    }
    return 1;
}

static int live(void) {
    return live_below(n_blocks);
}

static int push_block(int kind, int run, int line) {
    if (n_blocks == BLOCKS) {
        fail("blocks inside blocks, more than 8 deep", "");
        return 0;
    }
    b_kind[n_blocks] = kind;
    b_run[n_blocks] = run;
    b_taken[n_blocks] = run;
    b_line[n_blocks] = line;
    b_text[n_blocks] = NULL;
    b_pos[n_blocks] = 0u;
    b_name[(unsigned)n_blocks * NAME_MAX] = 0;
    n_blocks++;
    return 1;
}

static void pop_block(void) {
    n_blocks--;
    if (b_text[n_blocks] != NULL) {
        free(b_text[n_blocks]);
        b_text[n_blocks] = NULL;
    }
}

/* --- what a test says ------------------------------------------------------------
 *
 * `$a == b`, the numbers with -lt -le -gt -ge, `-e -d -f path`, or a command,
 * which is true when its status is 0. The words are already expanded. */

static int compare(char *a, char *op, char *b, int *out) {
    int left;
    int right;
    if (strcmp(op, "==") == 0) {
        *out = (strcmp(a, b) == 0) ? 1 : 0;
        return 1;
    }
    if (strcmp(op, "!=") == 0) {
        *out = (strcmp(a, b) != 0) ? 1 : 0;
        return 1;
    }
    left = atoi(a);
    right = atoi(b);
    if (strcmp(op, "-lt") == 0) { *out = (left < right) ? 1 : 0; return 1; }
    if (strcmp(op, "-le") == 0) { *out = (left <= right) ? 1 : 0; return 1; }
    if (strcmp(op, "-gt") == 0) { *out = (left > right) ? 1 : 0; return 1; }
    if (strcmp(op, "-ge") == 0) { *out = (left >= right) ? 1 : 0; return 1; }
    return 0;
}

static void run_command_words(int count, char **w);

static int test_words(int count, char **w, int *out) {
    sys_stat_t st;
    int kind;

    if (count == 0) {
        fail("nothing to test", "");
        return 0;
    }
    if (count >= 2 && (strcmp(w[0], "-e") == 0 || strcmp(w[0], "-d") == 0
                       || strcmp(w[0], "-f") == 0)) {
        kind = (stat(w[1], &st) >= 0) ? (int)st.type : 0;
        if (strcmp(w[0], "-e") == 0) *out = (kind != 0) ? 1 : 0;
        else if (strcmp(w[0], "-d") == 0) *out = (kind == S_DIR) ? 1 : 0;
        else *out = (kind == S_FILE) ? 1 : 0;
        return 1;
    }
    if (count == 3 && compare(w[0], w[1], w[2], out) != 0) return 1;
    if (count == 2 && (strcmp(w[1], "==") == 0 || strcmp(w[1], "!=") == 0
                       || strcmp(w[1], "-lt") == 0 || strcmp(w[1], "-le") == 0
                       || strcmp(w[1], "-gt") == 0 || strcmp(w[1], "-ge") == 0)) {
        fail("nothing to compare with: ", w[1]);
        return 0;
    }
    run_command_words(count, w);                /* a command: 0 is true */
    *out = (last_status == 0) ? 1 : 0;
    return 1;
}

/* --- let: whole numbers, + - * / % and ( ) as their own words --------------------- */

static int expr_at(int *i, int count, char **w, int *out);

static int factor_at(int *i, int count, char **w, int *out) {
    char *word;
    if (*i >= count) {
        fail("the sum stops short", "");
        return 0;
    }
    word = w[*i];
    if (strcmp(word, "(") == 0) {
        *i = *i + 1;
        if (!expr_at(i, count, w, out)) return 0;
        if (*i >= count || strcmp(w[*i], ")") != 0) {
            fail("no ) in the sum", "");
            return 0;
        }
        *i = *i + 1;
        return 1;
    }
    if (*word != '-' && *word != '+' && isdigit((int)*word) == 0) {
        fail("not a number: ", word);
        return 0;
    }
    if ((*word == '-' || *word == '+') && isdigit((int)word[1]) == 0) {
        fail("not a number: ", word);
        return 0;
    }
    *out = atoi(word);
    *i = *i + 1;
    return 1;
}

static int term_at(int *i, int count, char **w, int *out) {
    int right;
    if (!factor_at(i, count, w, out)) return 0;
    while (*i < count && (strcmp(w[*i], "*") == 0 || strcmp(w[*i], "/") == 0
                          || strcmp(w[*i], "%") == 0)) {
        char *op = w[*i];
        *i = *i + 1;
        if (!factor_at(i, count, w, &right)) return 0;
        if (strcmp(op, "*") == 0) {
            *out = *out * right;
        } else {
            if (right == 0) {
                fail("divide by zero", "");
                return 0;
            }
            *out = (strcmp(op, "/") == 0) ? (*out / right) : (*out % right);
        }
    }
    return 1;
}

static int expr_at(int *i, int count, char **w, int *out) {
    int right;
    if (!term_at(i, count, w, out)) return 0;
    while (*i < count && (strcmp(w[*i], "+") == 0 || strcmp(w[*i], "-") == 0)) {
        char *op = w[*i];
        *i = *i + 1;
        if (!term_at(i, count, w, &right)) return 0;
        *out = (strcmp(op, "+") == 0) ? (*out + right) : (*out - right);
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
    if (strcmp(word, "graphics") == 0) {
        graphics_mode = 1;
        return;
    }
    fail("no such setting: ", word);
}

/* A standalone unquoted >, >> or < in a run of text. */
static int redirect_word(char *s) {
    int quoted = 0;
    char *p = s;
    while (*p != 0) {
        if (*p == '"') quoted = (quoted == 0) ? 1 : 0;
        if (quoted == 0 && p[0] == ';' && p[1] == ';') return 0;
        if (quoted == 0 && (*p == '>' || *p == '<')
                && (p == s || p[-1] == ' ' || p[-1] == '\t')) {
            char *q = p;
            while (*q == '>' || *q == '<') q++;
            if (*q == ' ' || *q == '\t') return 1;
        }
        p++;
    }
    return 0;
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
    /* `$x = $(ls) > out.txt` reads as a redirection and is not one: the whole
     * right-hand side is the value. Rather than quietly making the value
     * "... > out.txt", say so (docs/redirect_plan.md Q3). */
    if (redirect_word(p)) {
        fail("a value cannot redirect: quote it if you meant the text", "");
        return 1;
    }
    if (!expand_value(p, value_buf, VALUE_MAX)) return 1;
    var_set(name, value_buf);
    return 1;
}

/* `>` `>>` and `<` taken out of the words, with the file each names. They
 * count only as whole words and only unquoted, so `echo ">"` is text
 * (docs/redirect_plan.md 3.3). 0 and a message when a file is missing. */
static int take_redirects(int *count, char **w) {
    char *word;
    int i = 0;
    int keep = 0;

    redirect_out = NULL;
    redirect_in = NULL;
    redirect_how = R_TRUNC;
    while (i < *count) {
        word = w[i];
        if (was_quoted[i] == 0
                && (strcmp(word, ">") == 0 || strcmp(word, ">>") == 0
                    || strcmp(word, "<") == 0)) {
            if (i + 1 >= *count) {
                fail("no file after ", word);
                return 0;
            }
            if (word[0] == '<') {
                redirect_in = w[i + 1];
            } else {
                redirect_out = w[i + 1];
                redirect_how = (word[1] == '>') ? R_APPEND : R_TRUNC;
            }
            i = i + 2;
            continue;
        }
        w[keep] = word;
        was_quoted[keep] = was_quoted[i];
        keep++;
        i++;
    }
    *count = keep;
    w[keep] = NULL;
    return 1;
}

/* A builtin's `>`: the same rule the kernel uses for a program, so one line
 * of the docs covers both -- `file~` is written and renamed over `file` when
 * the builtin is done, and `>>` adds to the end. */
static int open_out(char *file, char *temp, unsigned room) {
    if (redirect_how == R_APPEND) {
        temp[0] = 0;
        return open(file, O_WRITE | O_CREATE | O_APPEND);
    }
    strlcpy(temp, file, room);
    strlcat(temp, "~", room);
    return open(temp, O_WRITE | O_CREATE | O_TRUNC);
}

static void close_out(int fd, char *file, char *temp) {
    close(fd);
    if (temp[0] == 0) return;
    remove(file);                       /* it may not be there at all */
    if (rename(temp, file) < 0) {
        remove(temp);
        warn(file, ": could not be written");
        last_status = 1;
    }
}

static void run_command_words(int count, char **w) {
    char path[PATH];
    char temp[PATH + 2];
    char number[STR_UTOA_MAX];
    int fd;

    if (count <= 0) return;
    started = 1;
    graphics_start();
    if (!take_redirects(&count, w)) return;
    if (count == 0) {
        fail("nothing to run", "");
        return;
    }
    if (is_builtin(w[0])) {
        if (redirect_in != NULL) {
            fail("< on ", w[0]);        /* no builtin reads a file this way */
            return;
        }
        if (redirect_out != NULL && capturing == 0) {
            fd = open_out(redirect_out, temp, sizeof(temp));
            if (fd < 0) {
                warn(redirect_out, ": could not be written");
                last_status = fd;
                return;
            }
            out_fd = fd;
            run_builtin(count, w);
            out_fd = -1;
            close_out(fd, redirect_out, temp);
        } else {
            run_builtin(count, w);
        }
    } else if (!find_program(w[0], path)) {
        warn(w[0], ": not found");
        last_status = E_NOENT;
    } else if (redirect_out != NULL || redirect_in != NULL) {
        last_status = exec_io(path, count, w, redirect_in, redirect_out, redirect_how);
        if (last_status == E_NOENT) {
            /* find_program has been past the program, so it is the file. */
            warn((redirect_in != NULL) ? redirect_in : redirect_out, ": not found");
        }
    } else if (graphics_mode != 0) {
        /* Its output into a buffer that is thrown away: on a screen the
         * script owns, only graphics.bin draws (docs/graphics_plan.md 4.2). */
        last_status = exec_out(path, count, w, gfx_bin, sizeof(gfx_bin));
    } else {
        last_status = exec(path, count, w);
    }
    /* A test asks a command how it went; it is never the end of the script. */
    if (last_status != 0 && stop_on_error != 0 && in_test == 0 && running != 0) {
        itoa(last_status, number);
        fail("stopped: ", number);
        status_out = last_status;
    }
}

static void run_command(char *line) {
    int count = split_words(line, words, store, STORE, WORDS, 0);
    if (count < 0) return;
    run_command_words(count, words);
}

/* --- the block words -------------------------------------------------------------
 *
 * These are read before anything is expanded, so a block inside a part that
 * isn't running is still counted, and an unset name in a branch that never
 * runs is not a mistake. */

/* The next line of a for's value into its variable: 0 when there are none
 * left. */
static int next_item(int b) {
    char *value = b_text[b];
    unsigned start;
    unsigned stop;
    char save;
    int ok;

    if (value == NULL) return 0;
    start = b_pos[b];
    if (value[start] == 0) return 0;
    stop = start;
    while (value[stop] != 0 && value[stop] != '\n') stop++;
    save = value[stop];
    value[stop] = 0;
    ok = var_set(b_name + (unsigned)b * NAME_MAX, value + start);
    value[stop] = save;
    b_pos[b] = (save == 0) ? stop : stop + 1u;
    return ok;
}

static int ask(char *rest, int *yes) {
    int count = split_words(rest, words, store, STORE, WORDS, 0);
    int ok;
    if (count < 0) return 0;
    in_test = 1;
    ok = test_words(count, words, yes);
    in_test = 0;
    return ok;
}

static void do_if(char *rest) {
    int parent = live();
    int yes = 0;
    if (parent != 0 && !ask(rest, &yes)) return;
    push_block(B_IF, (parent != 0 && yes != 0) ? 1 : 0, at_index);
}

static void do_else(void) {
    int top = n_blocks - 1;
    if (n_blocks == 0 || b_kind[top] != B_IF) {
        fail("an else with no if", "");
        return;
    }
    b_run[top] = (live_below(top) != 0 && b_taken[top] == 0) ? 1 : 0;
    if (b_run[top] != 0) b_taken[top] = 1;
}

static void do_while(char *rest) {
    int parent = live();
    int yes = 0;
    if (parent != 0 && !ask(rest, &yes)) return;
    push_block(B_WHILE, (parent != 0 && yes != 0) ? 1 : 0, at_index);
}

/* for $name in VALUE: the value is taken once, and walked a line at a time. */
static void do_for(char *rest) {
    char name[NAME_MAX];
    char *p = trim(rest);
    char *copy;
    unsigned n = 0u;
    int parent = live();
    int here;

    if (*p != '$') {
        fail("for wants $name in ...", "");
        return;
    }
    p++;
    while (is_name_char((int)p[n]) != 0 && n + 1u < NAME_MAX) {
        name[n] = p[n];
        n++;
    }
    name[n] = 0;
    p = p + n;
    while (*p == ' ' || *p == '\t') p++;
    if (n == 0u || p[0] != 'i' || p[1] != 'n' || (p[2] != ' ' && p[2] != '\t')) {
        fail("for wants $name in ...", "");
        return;
    }
    p = p + 2;
    while (*p == ' ' || *p == '\t') p++;    /* not part of the first line */
    if (parent == 0) {
        push_block(B_FOR, 0, at_index);
        return;
    }
    if (!expand_value(p, value_buf, VALUE_MAX)) return;
    if (!push_block(B_FOR, 1, at_index)) return;
    here = n_blocks - 1;
    copy = (char *)malloc(strlen(value_buf) + 1u);
    if (copy == NULL) {
        pop_block();
        fail("no room for what for walks", "");
        return;
    }
    strcpy(copy, value_buf);
    b_text[here] = copy;
    strlcpy(b_name + (unsigned)here * NAME_MAX, name, NAME_MAX);
    if (next_item(here) == 0) b_run[here] = 0;      /* nothing to walk: no turns */
}

static void do_end(void) {
    int top = n_blocks - 1;

    if (n_blocks == 0) {
        fail("an end with no if, while or for", "");
        return;
    }
    if (b_kind[top] == B_WHILE && b_run[top] != 0) {
        jump_to = b_line[top];              /* the while line again, test and all */
        pop_block();
        return;
    }
    if (b_kind[top] == B_FOR && b_run[top] != 0 && next_item(top) != 0) {
        jump_to = b_line[top] + 1;          /* the body again, the value kept */
        return;
    }
    pop_block();
}

static void do_break(void) {
    int i;
    if (live() == 0) return;                /* in a part that isn't running */
    for (i = n_blocks - 1; i >= 0; i--) {
        if (b_kind[i] == B_WHILE || b_kind[i] == B_FOR) {
            b_run[i] = 0;                   /* the rest of the body is skipped,
                                             * and end will not go round again */
            return;
        }
    }
    fail("a break outside a while or for", "");
}

/* let $name = a sum of whole numbers, ( ) their own words. */
static void do_let(char *rest) {
    char name[NAME_MAX];
    char number[STR_UTOA_MAX];
    char *p = trim(rest);
    unsigned n = 0u;
    int count;
    int i = 0;
    int value = 0;

    if (*p != '$') {
        fail("let wants $name = ...", "");
        return;
    }
    p++;
    while (is_name_char((int)p[n]) != 0 && n + 1u < NAME_MAX) {
        name[n] = p[n];
        n++;
    }
    name[n] = 0;
    p = p + n;
    while (*p == ' ' || *p == '\t') p++;
    if (n == 0u || *p != '=') {
        fail("let wants $name = ...", "");
        return;
    }
    p++;
    count = split_words(p, words, store, STORE, WORDS, 0);
    if (count < 0) return;
    if (!expr_at(&i, count, words, &value)) return;
    if (i != count) {
        fail("more after the sum: ", words[i]);
        return;
    }
    itoa(value, number);
    var_set(name, number);
}

/* read $name: a typed line into a variable. A keyword, not a builtin, for
 * the same reason let is one -- the name must not be expanded before it is
 * set, and there is nothing sensible for $(read $x) to mean. */
static void do_read(char *rest) {
    char input[256];
    char *p = trim(rest);
    int n;

    if (*p != '$' || is_name_char((int)p[1]) == 0) {
        fail("read wants $name", "");
        return;
    }
    n = read(STDIN, input, sizeof(input) - 1u);
    if (n < 0) {
        warn("read: ", sys_strerror(n));
        last_status = 1;
        return;
    }
    input[n] = 0;
    while (n > 0 && (input[n - 1] == '\n' || input[n - 1] == '\r')) {
        n--;
        input[n] = 0;
    }
    if (var_set(p + 1, input)) last_status = 0;
}

/* The first word of a line, as written. Keywords are short, so a longer one
 * is cut and matches nothing, which is all this is for. */
static void first_word(char *line, char *out, unsigned room) {
    unsigned n = 0u;
    while (line[n] != 0 && line[n] != ' ' && line[n] != '\t' && n + 1u < room) {
        out[n] = line[n];
        n++;
    }
    out[n] = 0;
}

static void do_line(char *line) {
    char word[16];
    char *p = trim(line);
    char *rest;

    if (*p == 0) return;
    if (p[0] == ';' && p[1] == ';') return;
    first_word(p, word, sizeof(word));
    rest = p + strlen(word);
    while (*rest == ' ' || *rest == '\t') rest++;

    if (strcmp(word, "if") == 0) { started = 1; do_if(rest); return; }
    if (strcmp(word, "else") == 0) { do_else(); return; }
    if (strcmp(word, "end") == 0) { do_end(); return; }
    if (strcmp(word, "while") == 0) { started = 1; do_while(rest); return; }
    if (strcmp(word, "for") == 0) { started = 1; do_for(rest); return; }
    if (strcmp(word, "break") == 0) { do_break(); return; }

    if (live() == 0) return;            /* skipped, and not even expanded */
    if (*p == '#') { directive(p); return; }
    if (strcmp(word, "let") == 0) { started = 1; do_let(rest); return; }
    if (strcmp(word, "read") == 0) { started = 1; do_read(rest); return; }
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

/* The end of the line that starts at p: the first newline not held open by
 * a backslash. Each one it passes becomes two spaces, so the words split as
 * though the whole thing had been typed on one line and every other byte
 * stays where it is -- the lines are cut in place. *physical counts what it
 * swallowed, so a message still names the line the script has.
 *
 * A backslash inside quotes is text, and so is one before anything but a
 * newline: `\$`, `\\` and `\"` are the script's own escapes. A comment runs
 * to the end of its line, and cannot continue it. */
static char *logical_end(char *p, unsigned *physical) {
    int quoted = 0;

    while (*p != 0) {
        if (*p == '\n') return p;
        if (quoted == 0 && p[0] == ';' && p[1] == ';') {
            while (*p != 0 && *p != '\n') p++;
            return p;
        }
        if (*p == '"') {
            quoted = (quoted == 0) ? 1 : 0;
            p++;
        } else if (*p == '\\' && p[1] == '\n' && quoted == 0) {
            p[0] = ' ';
            p[1] = ' ';
            *physical = *physical + 1u;
            p = p + 2;
        } else if (*p == '\\' && p[1] != 0 && p[1] != '\n') {
            p = p + 2;                  /* an escape, never over a newline */
        } else {
            p++;
        }
    }
    return p;
}

/* The script's lines, cut in place. A last line with no newline counts. */
static int split_lines(void) {
    char *p = text;
    char *end;
    unsigned physical = 1u;

    n_lines = 0;
    while (1) {
        if (n_lines == LINES_MAX) {
            say("pgs: over 1024 lines\n");
            return 0;
        }
        lines[n_lines] = p;
        line_no[n_lines] = physical;
        n_lines++;
        end = logical_end(p, &physical);
        if (*end == 0) break;
        *end = 0;
        physical++;
        p = end + 1;
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
    out_fd = -1;                        /* nothing redirected yet */
    script = argv[1];
    args = argv + 1;                    /* $0 is the script, $1.. its arguments */
    n_args = argc - 2;
    if (!load_script(script)) return 1;
    if (!split_lines()) return 1;

    running = 1;
    i = 0;
    while (i < n_lines && running != 0) {
        at = line_no[i];
        at_index = i;
        jump_to = -1;
        do_line(lines[i]);
        i = (jump_to >= 0) ? jump_to : i + 1;
    }
    if (running != 0 && n_blocks > 0) {
        char *kind = "for";
        if (b_kind[n_blocks - 1] == B_IF) kind = "if";
        if (b_kind[n_blocks - 1] == B_WHILE) kind = "while";
        at = line_no[b_line[n_blocks - 1]];
        fail("no end for this ", kind);
    }
    return status_out;
}
