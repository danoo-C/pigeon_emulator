/* ls -- list directories, sorted by name (docs/phase4_plan.md step 7).
 *
 *     ls [-l] [DIR...]
 *
 * With no DIR, the current directory. A directory's name ends in '/'. -l
 * puts each file's size in front of its name, and <dir> in front of a
 * directory's. Up to 256 entries are sorted; any after those follow in the
 * directory's own order.
 */
#include <pigeon/mem.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define SORTED 256

static sys_stat_t *table;                   /* SORTED entries, on the heap */
static sys_stat_t *order[SORTED];

static void show(sys_stat_t *st, int long_form) {
    if (long_form) {
        if (st->type == S_DIR) printf("%8s %s/\n", "<dir>", st->name);
        else printf("%8u %s\n", st->size, st->name);
    } else if (st->type == S_DIR) {
        printf("%s/\n", st->name);
    } else {
        printf("%s\n", st->name);
    }
}

static int list(char *dir, int long_form) {
    sys_stat_t later;
    sys_stat_t *moving;
    int count = 0;
    int i;
    int j;
    int dh = opendir(dir);
    if (dh < 0) {
        printf("ls: %s: %s\n", dir, sys_strerror(dh));
        return 1;
    }
    while (count < SORTED && readdir(dh, &table[count]) == 1) {
        order[count] = &table[count];
        count++;
    }
    for (i = 1; i < count; i++) {           /* insertion sort, by name */
        moving = order[i];
        j = i - 1;
        while (j >= 0 && strcmp(order[j]->name, moving->name) > 0) {
            order[j + 1] = order[j];
            j--;
        }
        order[j + 1] = moving;
    }
    for (i = 0; i < count; i++) show(order[i], long_form);
    if (count == SORTED) {
        while (readdir(dh, &later) == 1) show(&later, long_form);
    }
    closedir(dh);
    return 0;
}

int main(int argc, char **argv) {
    int i;
    int long_form = 0;
    int dirs = 0;
    int failed = 0;
    for (i = 1; i < argc; i++) {
        if (argv[i][0] != '-') {
            dirs++;
        } else if (strcmp(argv[i], "-l") == 0) {
            long_form = 1;
        } else {
            printf("ls: unknown option %s\n", argv[i]);
            return 1;
        }
    }
    table = (sys_stat_t *)malloc(SORTED * sizeof(sys_stat_t));
    if (table == NULL) {
        print("ls: no memory\n");
        return 1;
    }
    if (dirs == 0) return list(".", long_form);
    for (i = 1; i < argc; i++) {
        if (argv[i][0] == '-') continue;
        if (dirs > 1) printf("%s:\n", argv[i]);
        failed = failed | list(argv[i], long_form);
    }
    return failed;
}
