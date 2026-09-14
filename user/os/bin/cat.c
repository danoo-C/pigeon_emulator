/* cat -- print each file named, one after another. */
#include <pigeon/sys.h>

int main(int argc, char **argv) {
    char buf[256];
    int i;
    int fd;
    int n;
    int failed = 0;
    for (i = 1; i < argc; i++) {
        fd = open(argv[i], O_READ);
        if (fd < 0) {
            print("cat: ");
            print(argv[i]);
            print(": ");
            print(sys_strerror(fd));
            print("\n");
            failed = 1;
            continue;
        }
        n = read(fd, buf, 256u);
        while (n > 0) {
            write(STDOUT, buf, (unsigned)n);
            n = read(fd, buf, 256u);
        }
        close(fd);
    }
    return failed;
}
