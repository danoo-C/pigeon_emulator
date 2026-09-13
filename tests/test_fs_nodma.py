"""lib/pigeon/fs.c on disks WITHOUT DMA: all of tests/test_fs.py, a second time.

Filesystem phase 6 gave the HDD commands 6 and 7, and fs.c uses them when
a disk answers its probe. The window path they replaced is still there --
for an HDD from before them, for the CD drive, and for any transfer a DMA
disk refuses, such as fs_load() straight into the framebuffer -- and once
DMA is what every Machine has, nothing else would ever run it.

So every test in test_fs.py runs here again, on a machine whose disks have
no RAM, which is exactly how an HDD without DMA is built. The two runs have
to agree on everything, including the oracle tests that compare disk images
with tools/pfs.py byte for byte and the crash test's every-write snapshots.

    python3 tests/test_fs_nodma.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_fs                                                        # noqa: E402
from test_fs import *                                                 # noqa: E402,F401,F403
from _runner import run_module                                        # noqa: E402

_with_dma = test_fs.machine_for


def _without_dma(disk, boot=None, disc=None):
    machine = _with_dma(disk, boot, disc)
    machine.hdd.ram = None
    if machine.user_prog is not None:
        machine.user_prog.ram = None
    return machine


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _no_dma(monkeypatch):
        # Every test here -- the ones imported from test_fs included --
        # finds machine_for through test_fs's own globals, so that is where
        # it has to be replaced.
        monkeypatch.setattr(test_fs, "machine_for", _without_dma)
        yield
except ImportError:
    pass


def test_dma_really_is_off_in_this_file():
    """If the replacement above ever stopped applying, every test in this
    file would quietly run on DMA a second time and prove nothing new."""
    commands = []
    with test_fs.disks() as d:
        disk = test_fs.formatted(d)
        machine = test_fs.machine_for(disk)
        channel = machine.io_controller.channels[2]
        original = channel.callback

        def recording(read_write, command, length, address, data):
            commands.append(command)
            return original(read_write, command, length, address, data)

        channel.callback = recording
        try:
            test_fs.load(machine, test_fs.program("""
                unsigned char *buf = (unsigned char *)malloc(20000u);
                TRY(fs_mount(CH_HDD));
                fill(buf, 20000u);
                if (fs_save("/big", buf, 20000u) != 20000) return -1;
                if (fs_load("/big", buf, 20000u) != 20000) return -2;
                return mismatches(buf, 20000u);"""))
            with contextlib.redirect_stdout(io.StringIO()):
                machine.run(deadline=time.time() + 60)
            assert machine.cpu.halted, "did not halt"
            assert machine.cpu.reg.read(0) == 0, "the round trip came back wrong"
        finally:
            machine.close()
    assert 7 not in commands, "a WRITE_DMA reached a disk without DMA"
    assert commands.count(6) == 1, f"expected exactly one probe, saw {commands.count(6)}"
    assert 2 in commands and 3 in commands, "the window path never ran"


if __name__ == "__main__":
    test_fs.machine_for = _without_dma
    raise SystemExit(run_module(dict(globals()), "fs.c without DMA"))
