/* echo -- print the words it was given, with a space between. */
#include <pigeon/sys.h>

int main(int argc, char **argv) {
    int i;
    for (i = 1; i < argc; i++) {
        if (i > 1) print(" ");
        print(argv[i]);
    }
    print("\n");
    return 0;
}
