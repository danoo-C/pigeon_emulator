#!/usr/bin/env sh
# Run the emulator under PyPy instead of CPython.
#
#     ./run-pypy.sh cube --run
#     ./run-pypy.sh cube --run --headless
#     ./run-pypy.sh --list
#
# Every argument is passed straight to start_emulator.py -- this only
# swaps the interpreter underneath it.
#
# Why: PyPy's JIT compiles the fetch/dispatch loop in emulator/machine.py
# to machine code, which is worth roughly 11-15x on real programs
# (cube.bin: 2.7M -> ~40M IPS). The interpreter is pure stdlib on the hot
# path, so nothing in the emulator had to change to get it.
#
# The catch is warm-up: for the first several seconds PyPy is SLOWER than
# CPython while the JIT is still tracing. The [IPS] readout climbs from
# ~7M to ~40M over about ten seconds. Judge throughput from the steady
# state, never from the first line -- that is the same mistake that makes
# tools/bench.py report PyPy as a regression, since its measurement
# window closes before the JIT has done anything.
#
# .pypy/ is gitignored (151 MB), so a fresh clone will not have it. The
# error below has the commands to put it back.
set -e

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
pypy="$here/.pypy/bin/pypy3"

if [ ! -x "$pypy" ]; then
    cat >&2 <<EOF
run-pypy.sh: no PyPy interpreter at
  $pypy

Install it. Everything lands inside this directory: it never touches the
system python3, your PATH, your shell config, or any other project.

  url=https://downloads.python.org/pypy/pypy3.11-v7.3.23-linux64.tar.gz
  mkdir -p "$here/.pypy"
  curl -sSL "\$url" | tar -xz -C "$here/.pypy" --strip-components=1

PyPy ships without pip, and it keeps its own site-packages -- separate
from .venv, so the display server needs its dependencies installed again
here. Skip these two if you only ever run --headless:

  "$pypy" -m ensurepip
  "$pypy" -m pip install -r "$here/requirements.txt"

To remove PyPy again, in full:  rm -rf "$here/.pypy"
EOF
    exit 1
fi

# exec, not a plain call: the emulator runs until you interrupt it, and
# this way Ctrl-C reaches it directly instead of killing the wrapper and
# orphaning the display and HID server threads.
#
# -u because this program is normally killed rather than allowed to exit.
# Redirect it to a file without -u and stdout is block-buffered, so the
# [IPS] readout sits in a 8 KB buffer that SIGTERM discards -- you get an
# empty log. On a terminal it makes no difference.
exec "$pypy" -u "$here/start_emulator.py" "$@"
