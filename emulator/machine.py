"""The machine itself: RAM, CPU, IO bus, and the devices hanging off it.

Constructing a Machine has no side effects beyond allocating RAM and
opening the disk images -- it prompts for nothing, prints nothing, and
binds no ports. Starting the HTTP servers is a separate, explicit call.
That is what makes it importable from tests. The interactive front end
lives in cli.py.
"""
import logging
import struct
import time
from typing import Optional

from .bios import BIOS
from .cpu import _HANDLERS, _UNPACK, CPU
from .instruction_set import INSTR_SIZE
from .devices.cd import CD
from .devices.display_io import DisplayIO
from .devices.hdd import HDD
from .devices.hid import HID
from .devices.timer import Timer
from .io_controller import IOChannel, IOController
from .memory_map import (
    CH_CD, CH_DISPLAY, CH_HDD, CH_HID, CH_TIMER, CH_USERPROG, RAM_SIZE,
    REGISTER_COUNT,
)
from .ram import RAM

log = logging.getLogger(__name__)

DISPLAY_FPS = 30

# Instructions between clock reads. Small enough that a 30 FPS frame
# deadline is never overshot (at ~2.4M IPS this is ~4 ms), large enough
# that time.time() costs nothing measurable per instruction.
CLOCK_SAMPLE_INTERVAL = 10_000


class Machine:
    """A wired-up pigeon computer, ready to run."""

    def __init__(self, bios_path="build/bios.bin", program_path=None,
                 disk_path=None, ram_size=RAM_SIZE, cd=None):
        self.ram = RAM(ram_size)
        self.cpu = CPU(self.ram, REGISTER_COUNT)
        self.io_controller = IOController(self.ram)

        # Channel 1: the disk the BIOS boots the user program from. Omitting
        # a program leaves the channel unregistered rather than opening the
        # current directory as a disk image, which is what an empty path
        # used to do (IsADirectoryError, from the "leave blank to skip" prompt).
        self.user_prog: Optional[HDD] = None
        if program_path:
            self.user_prog = HDD(program_path)
            self.io_controller.register_channel(
                CH_USERPROG, IOChannel(self.user_prog.callback, name="USERPROG"))
        else:
            log.warning("No user program: channel %d is empty, the BIOS will "
                        "boot into whatever is at the load address", CH_USERPROG)

        self.hdd = HDD(disk_path)
        self.hid = HID()
        self.timer = Timer()
        # Registered unconditionally, unlike CH_USERPROG: an unregistered
        # channel answers 0xFFFFFFFF, which is the right answer for a
        # machine with no drive and the wrong one for a drive with no
        # disc. Always present, the guest can tell those apart -- which is
        # what <pigeon/cd.h> uses to run on a machine built before this
        # device existed. It starts empty; the host puts a disc in.
        # Passed in when there is a Config to build it from (cli.py), so
        # this module needs to know nothing about config.json.
        self.cd = cd if cd is not None else CD()
        # Constructed before the loop below, not after: it is a device on
        # the bus now (scanout base, framebuffer fill), not only the thing
        # that serves frames over HTTP.
        self.display_io = DisplayIO(self.ram)
        for channel_id, device, name in (
            (CH_HDD, self.hdd, "HDD"),
            (CH_HID, self.hid, "HID"),
            (CH_TIMER, self.timer, "TIMER"),
            (CH_DISPLAY, self.display_io, "DISPLAY"),
            (CH_CD, self.cd, "CD"),
        ):
            self.io_controller.register_channel(
                channel_id, IOChannel(device.callback, name=name))

        self.bios = BIOS.from_file(bios_path)
        self.bios.write_bios(self.ram)

        self.instruction_count = 0
        self.total_instructions = 0
        self.last_ips = 0.0

    # --- lifecycle --------------------------------------------------------

    def start_servers(self, host="127.0.0.1", display_port=8000, hid_port=8001,
                      cd_port=8002):
        """Bind the display, input and CD HTTP servers. Separate from
        __init__ so a test can build a Machine without touching the
        network."""
        self.display_io.hid_url = f"http://{host}:{hid_port}"
        self.display_io.cd_url = f"http://{host}:{cd_port}"
        self.display_io.start_fastapi(host=host, port=display_port)
        # The browser front end is served from the display port and posts
        # input to the HID port, so the display origin has to be on the HID
        # server's allow list or every POST fails CORS preflight.
        self.hid.start_fastapi(
            host=host, port=hid_port,
            allow_origins=[f"http://{host}:{display_port}",
                           f"http://{host}:{hid_port}",
                           f"http://localhost:{display_port}"])
        # Same reason, same trap: the page lives on the display port.
        self.cd.start_fastapi(
            host=host, port=cd_port,
            allow_origins=[f"http://{host}:{display_port}",
                           f"http://{host}:{cd_port}",
                           f"http://localhost:{display_port}"])

    def close(self):
        """Release the disk file handles."""
        for disk in (self.user_prog, self.hdd, self.cd):
            if disk is not None:
                disk.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- execution --------------------------------------------------------

    def step(self):
        """Execute one instruction and service any IO it triggered.

        Returns 1 once the CPU has halted, else 0.
        """
        if self.cpu.run() == 1:
            return 1
        if self.ram.io_pending:
            self.io_controller.update()
        return 0

    def run(self, on_frame=None, report_ips=None, deadline=None):
        """Run until HALT.

        `on_frame` is called at DISPLAY_FPS with no arguments; `report_ips`
        is called about once a second with the measured rate. `deadline` is
        a time.time() value to stop at -- for benchmarking, so throughput
        can be measured on the same loop users actually run rather than on
        a step()-per-instruction imitation of it.
        """
        frame_interval = 1.0 / DISPLAY_FPS
        now = time.time()
        last_frame = last_ips = now
        self.instruction_count = 0
        countdown = CLOCK_SAMPLE_INTERVAL

        # step() is inlined below rather than called. At a couple of million
        # instructions a second, a method call that does one `if` is about
        # 9% of total runtime -- it showed up third in the profile, above
        # every real instruction handler. step() itself stays exactly as it
        # is: the debugger and every test go through it, and this loop must
        # keep matching it.
        cpu = self.cpu
        ram = self.ram
        controller = self.io_controller
        memory = ram.mem
        handlers = _HANDLERS
        unpack = _UNPACK
        instruction_size = INSTR_SIZE

        # Counters are locals in the loop and written back at each sample
        # point. `self.x += 1` is a load, an add and a store through the
        # instance dict; done twice per instruction it cost more than most
        # of the handlers.
        executed = 0

        while True:
            if cpu.halted:
                break

            pc = cpu.pc
            # try:
            opcode, dst, src1, src2, imm = unpack(memory, pc)
            # except struct.error:
            #     raise RuntimeError(f"Fetch past end of memory at PC={pc:#06x}") from None
            cpu.pc = pc + instruction_size

            handler = handlers[opcode]
            if handler is None:
                raise RuntimeError(f"Unknown opcode {opcode} at PC={pc:#06x}")
            handler(cpu, dst, src1, src2, imm)

            if ram.io_pending:
                controller.update()

            # Reading the clock costs more than executing an instruction, so
            # do it once per CLOCK_SAMPLE_INTERVAL rather than twice per
            # instruction. A countdown beats a modulo on this path.
            countdown -= 1
            if countdown:
                continue
            executed += CLOCK_SAMPLE_INTERVAL
            self.instruction_count += CLOCK_SAMPLE_INTERVAL
            self.total_instructions += CLOCK_SAMPLE_INTERVAL
            countdown = CLOCK_SAMPLE_INTERVAL

            now = time.time()
            if now - last_frame >= frame_interval:
                self.display_io.update()
                if on_frame is not None:
                    on_frame()
                last_frame = now

            if deadline is not None and now >= deadline:
                break

            elapsed = now - last_ips
            if elapsed >= 1.0:
                self.last_ips = self.instruction_count / elapsed
                if report_ips is not None:
                    report_ips(self.last_ips)
                self.instruction_count = 0
                last_ips = now

        # Count the partial window the loop ended in, so the totals are
        # exact rather than rounded down to the last sample point.
        remainder = CLOCK_SAMPLE_INTERVAL - countdown
        self.instruction_count += remainder
        self.total_instructions += remainder

    def dump_ram_to(self, path):
        """Write the whole address space out for debugging."""
        with open(path, "wb") as f:
            f.write(self.ram.dump_ram())
        return path
