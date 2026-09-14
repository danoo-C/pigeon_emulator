/* clear -- clear the screen: ESC [ 2 J, which the console understands. */
#include <pigeon/sys.h>

int main(void) {
    print("\x1b[2J");
    return 0;
}
