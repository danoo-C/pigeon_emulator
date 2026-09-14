# Kernel prototype

The evidence behind [docs/kernel.md](../../docs/kernel.md) §16 and
[docs/kernel_exec.md](../../docs/kernel_exec.md) §5. **Nothing here changes
the emulator.** `proto.py` adds six instructions — `GETSP`, `SETSP`, `EI`,
`DI`, `IRET`, `SETIV` — to the instruction and syntax tables at runtime, and
runs the CPU in its own loop, which can deliver interrupts and faults. Since
the kernel's phase 2 the emulator has these instructions itself; `proto.py`
still registers its own copies, which take opcodes 35–40, so every script
runs as it did.

Run the scripts from this folder. PyPy is about ten times faster:

```bash
cd prototypes/kernel
../../.pypy/bin/pypy3 p4_kernel.py      # or python3
```

| Script | Row in kernel.md §16 | What it checks |
|---|---|---|
| `smoke.py` | — | The harness itself: the new opcodes, and a relocated program called like a function |
| `p0_relocation.py` | kernel_exec.md §5 | Assembling twice finds every address in `user/`'s programs; a patched program runs, with its frame stack and heap after its image |
| `p1_tests.py` | P1 | The repository's tests with the new instructions loaded. Needs pytest, so use python3; about seven and a half minutes |
| `p2_interrupts.py` | P2 | Compiled C interrupted after every instruction, with two negative controls |
| `p3_io_header.py` | P3 | A handler doing IO while the program does IO, unprotected and two ways protected |
| `p4_kernel.py` | P4 | Kernel, shell, exec, `exit`, faults, break, programs running programs |
| `p5_disk.py` | P5 | P4 loading its programs from a PigeonFS image, plus `ls` |
| `p6_multitask.py` | P6 | Three programs preempted by a timer, with a negative control |
| `p7_cost.py` | P7 | What checking for interrupts costs. Run it under both interpreters |

Each script prints what it found. Negative controls are meant to come out
broken.

**How the pieces are built**

- **Kernels** are compiled as today, at `0x20000` with a `HALT` at the end.
  Hand-written assembly routines are inserted among the compiled code, and C
  reaches them as `extern int name;` plus a cast to a function pointer.
- **Programs** come from `build_app`: new startup code that returns instead
  of halting, the frame stack and heap placed after the image, and two
  assemblies compared. The file is a 32-byte header, the image, then the
  offsets to patch.
- **Vectors:** 0 divide by zero, 1 unknown opcode, 2 fetch past memory,
  3 timer, 4 break.
- **Device interrupts are raised by the scripts**, not by the timer and
  keyboard devices, which had no interrupts until phase 2.
