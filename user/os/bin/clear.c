/* clear -- clear the screen and the scrollback, as Linux's clear does:
 * ESC [ 3 J, then ESC [ 2 J, which the console understands
 * (docs/phase4b_plan.md step 4). */
#include <pigeon/sys.h>

int main(void) {
    print("\x1b[3J\x1b[2J");
    return 0;
}
