"""P5 (docs/kernel.md §16): P4's kernel, but programs come from a real PigeonFS disk.

The kernel includes fs.c, mounts CH_HDD, and exec loads /bin/<name>.bin with
fs_load straight into the program's place in memory (header just below it),
then patches it. An `ls` program lists /bin through a kernel call. The disk
image is built with tools/pfs.py."""
import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from proto import *  # noqa: F403
from emulator.devices.hdd import HDD
from emulator.io_controller import IOChannel, IOController
from emulator.memory_map import CH_HDD, HEAP_START

HERE = Path(__file__).resolve().parent
_src = (HERE / "p4_kernel.py").read_text()
exec(_src[:_src.index("kimg, ksym = build_fixed")])     # KERNEL, KASM, SYS_H, APPS, EXPECTED

NEW_EXEC = r'''
int k_exec(char *name, int argc, char **argv) {
    char path[48];
    unsigned *h;
    unsigned size;
    unsigned nrel;
    unsigned frame;
    unsigned base;
    unsigned i;
    unsigned delta;
    unsigned *rel;
    unsigned *p;
    int got;
    int status;
    if (depth + 1 >= MAXDEPTH) return -5;
    if (depth == 0) base = POOL;
    else base = (((*(unsigned *)procs[depth].heap_ptr_at) + 7u) & 0xFFFFFFF8u) + 32u;
    strlcpy(path, "/bin/", 48u);
    strlcat(path, name, 48u);
    strlcat(path, ".bin", 48u);
    got = fs_load(path, (void *)(base - 32u), TOP - base);
    if (got == FS_ENOENT) return -1;
    if (got < 0) return got;
    h = (unsigned *)(base - 32u);
    if ((unsigned)got < 32u || h[0] != MAGIC || h[1] != 1u) return -3;
    size = h[2];
    nrel = h[4];
    frame = h[5];
    if ((unsigned)got != 32u + size + nrel * 4u) return -3;
    if (base + size + frame + 0x10000u > TOP) return -4;
    rel = (unsigned *)(base + size);
    delta = base - h[7];
    for (i = 0u; i < nrel; i++) {
        p = (unsigned *)(base + rel[i]);
        *p = *p + delta;
    }
    depth++;
    if ((unsigned)depth > deepest) deepest = (unsigned)depth;
    if (nexec < 16u) {
        bases[nexec] = base;
        nexec = nexec + 1u;
    }
    procs[depth].base = base;
    procs[depth].end = base + size;
    procs[depth].heap_ptr_at = base + h[6];
    status = ((call4_fn)&exec_call)(base + h[3], argc, argv, &procs[depth].save_sp);
    depth--;
    return status;
}

int k_dirent(char *path, int index, char *out) {
    fs_stat_t st;
    int dh;
    int i;
    int r;
    dh = fs_opendir(path);
    if (dh < 0) return dh;
    r = 0;
    for (i = 0; i <= index; i++) {
        r = fs_readdir(dh, &st);
        if (r != 1) break;
    }
    fs_closedir(dh);
    if (r != 1) return 0;
    strlcpy(out, st.name, 32u);
    return 1;
}

'''

start = KERNEL.index("int k_exec(char *name")
end = KERNEL.index("void k_exit(int code)")
K9 = "#include <pigeon/fs.h>\n#include <pigeon/string.h>\n" + KERNEL[:start] + NEW_EXEC + KERNEL[end:]
K9 = K9.replace("extern int w_exit;", "extern int w_exit;\nextern int w_dirent;", 1)
K9 = K9.replace("    t[3] = (unsigned)&w_exit;", "    t[3] = (unsigned)&w_exit;\n    t[4] = (unsigned)&w_dirent;", 1)
K9 = K9.replace('    k_puts("kernel up\\n");',
                '    if (fs_mount(CH_HDD) < 0) {\n        k_puts("no disk\\n");\n        return -1;\n    }\n'
                '    k_puts("kernel up\\n");', 1)
assert "w_dirent" in K9 and "fs_mount" in K9
KASM9 = KASM + "\nw_dirent:\n    DI\n    CALL k_dirent\n    EI\n    RET\n"

DIRENT = r'''
typedef int (*dirent_fn)(char *, int, char *);
int sys_dirent(char *p, int i, char *out) {
    return ((dirent_fn)(*(unsigned *)(SYSTAB + 16u)))(p, i, out);
}
'''
APPS9 = dict(APPS)
APPS9["ls"] = SYS_H + DIRENT + r'''
int main(int argc, char **argv) {
    char name[32];
    char *dir;
    int i;
    dir = "/";
    if (argc > 1) dir = argv[1];
    i = 0;
    while (1) {
        if (sys_dirent(dir, i, name) != 1) break;
        sys_puts(name);
        sys_puts("\n");
        i++;
    }
    return i;
}
'''
old = '    a[0] = "hello"; a[1] = "world"; a[2] = (char *)0;\n    report("hello"'
assert old in APPS9["sh"]
APPS9["sh"] = APPS9["sh"].replace(
    old, '    a[0] = "ls"; a[1] = "/bin"; a[2] = (char *)0;\n'
         '    report("ls", sys_exec("ls", 2, a));\n' + old, 1)

kimg, ksym = build_fixed(K9, KASM9)
blobs = {name: build_app(text) for name, text in APPS9.items()}

work = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, work, True)
image = work / "hdd.img"


def pfs(*args):
    return subprocess.run([sys.executable, str(ROOT / "tools" / "pfs.py"), *args],
                          check=True, capture_output=True, text=True).stdout


pfs("mkfs", "--image", str(image), "--size", "4M")
pfs("mkdir", "--image", str(image), "/bin")
for name, blob in blobs.items():
    local = work / f"{name}.bin"
    local.write_bytes(blob)
    pfs("put", "--image", str(image), str(local), f"/bin/{name}.bin")
print("pfs ls /bin:", " ".join(pfs("ls", "--image", str(image), "/bin").split()))
print(f"kernel image {len(kimg):,} bytes (with fs.c); program files", {n: len(b) for n, b in blobs.items()})

REST = EXPECTED[EXPECTED.index("hello world argc=2"):]


def attempt(k):
    ram = RAM(RAM_SIZE)
    ram.load_bytes(kimg, PROGRAM_LOAD_ADDR)
    hdd = HDD(str(image), ram=ram)
    commands = {}

    def counted(read_write, command, length, address, data):
        commands[command] = commands.get(command, 0) + 1
        return hdd.callback(read_write, command, length, address, data)

    io = IOController(ram)
    io.register_channel(CH_HDD, IOChannel(counted, name="HDD"))
    cpu = new_cpu(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    timer = every(k) if k else None
    con = ksym["__g_console"]
    st = {"n": 0, "break_at": None, "sent": False}

    def tick(c, opcode):
        if timer:
            timer(c, opcode)
        st["n"] += 1
        if st["sent"]:
            return
        if st["break_at"] is None:
            if st["n"] % 500 == 0 and b"spin: looping" in ram.mem[con:con + 4096]:
                st["break_at"] = st["n"] + 20000
        elif st["n"] >= st["break_at"]:
            c.pending |= 1 << BREAK
            st["sent"] = True

    try:
        n = run(cpu, io=io, tick=tick, limit=300_000_000)
    finally:
        hdd.close()
    text = c_string(ram, con)
    lines = text.split("\n")
    listing = lines[2:2 + len(blobs)]
    rebuilt = ("kernel up\nsh: start, argv[0]=sh\n" + "".join(l + "\n" for l in listing)
               + f"ls -> {len(blobs)}\n" + REST)
    nexec = ram.read_word(ksym["__g_nexec"])
    return {
        "listing_ok": sorted(listing) == sorted(f"{name}.bin" for name in blobs),
        "console_ok": text == rebuilt,
        "text": text,
        "a": cpu.reg.read(0),
        "stack_ok": cpu.sp == STACK_TOP and cpu.reg.read(5) == HEAP_START,
        "ticks": ram.read_word(ksym["__g_ticks"]),
        "deepest": ram.read_word(ksym["__g_deepest"]),
        "execs": nexec,
        "hdd_commands": dict(sorted(commands.items())),
        "instructions": n,
    }


for k in (None, 13, 1):
    r = attempt(k)
    text = r.pop("text")
    print(f"timer every {k}: {r}")
    if not (r["console_ok"] and r["listing_ok"]):
        print(text)
