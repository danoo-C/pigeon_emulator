"""The machine itself: RAM, CPU, IO bus, and the devices hanging off it.

Constructing a Machine has no side effects beyond allocating RAM and
opening the disk images -- it prompts for nothing, prints nothing, and
binds no ports. Starting the HTTP servers is a separate, explicit call.
That is what makes it importable from tests. The interactive front end
lives in cli.py.
"""
import logging
import time
from typing import Optional

from .bios import BIOS
from .cpu import CPU
from .devices.display_io import DisplayIO
from .devices.hdd import HDD
from .devices.hid import HID
from .devices.timer import Timer
from .io_controller import IOChannel, IOController
from .memory_map import (
    CH_HDD, CH_HID, CH_TIMER, CH_USERPROG, RAM_SIZE, REGISTER_COUNT,
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
                 disk_path=None, ram_size=RAM_SIZE):
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
        for channel_id, device, name in (
            (CH_HDD, self.hdd, "HDD"),
            (CH_HID, self.hid, "HID"),
            (CH_TIMER, self.timer, "TIMER"),
        ):
            self.io_controller.register_channel(
                channel_id, IOChannel(device.callback, name=name))

        self.display_io = DisplayIO(self.ram)

        self.bios = BIOS.from_file(bios_path)
        self.bios.write_bios(self.ram)

        self.instruction_count = 0
        self.total_instructions = 0
        self.last_ips = 0.0

    # --- lifecycle --------------------------------------------------------

    def start_servers(self, host="127.0.0.1", display_port=8000, hid_port=8001):
        """Bind the display and input HTTP servers. Separate from __init__
        so a test can build a Machine without touching the network."""
        self.display_io.hid_url = f"http://{host}:{hid_port}"
        self.display_io.start_fastapi(host=host, port=display_port)
        # The browser front end is served from the display port and posts
        # input to the HID port, so the display origin has to be on the HID
        # server's allow list or every POST fails CORS preflight.
        self.hid.start_fastapi(
            host=host, port=hid_port,
            allow_origins=[f"http://{host}:{display_port}",
                           f"http://{host}:{hid_port}",
                           f"http://localhost:{display_port}"])

    def close(self):
        """Release the disk file handles."""
        for disk in (self.user_prog, self.hdd):
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

    def run(self, on_frame=None, report_ips=None):
        """Run until HALT.

        `on_frame` is called at DISPLAY_FPS with no arguments; `report_ips`
        is called about once a second with the measured rate.
        """
        frame_interval = 1.0 / DISPLAY_FPS
        now = time.time()
        last_frame = last_ips = now
        self.instruction_count = 0
        countdown = CLOCK_SAMPLE_INTERVAL

        while True:
            if self.step() == 1:
                break
            self.instruction_count += 1
            self.total_instructions += 1

            # Reading the clock costs more than executing an instruction, so
            # do it once per CLOCK_SAMPLE_INTERVAL rather than twice per
            # instruction. A countdown beats a modulo on this path.
            countdown -= 1
            if countdown:
                continue
            countdown = CLOCK_SAMPLE_INTERVAL

            now = time.time()
            if now - last_frame >= frame_interval:
                self.display_io.update()
                if on_frame is not None:
                    on_frame()
                last_frame = now

            elapsed = now - last_ips
            if elapsed >= 1.0:
                self.last_ips = self.instruction_count / elapsed
                if report_ips is not None:
                    report_ips(self.last_ips)
                self.instruction_count = 0
                last_ips = now

    def dump_ram_to(self, path):
        """Write the whole address space out for debugging."""
        with open(path, "wb") as f:
            f.write(self.ram.dump_ram())
        return path
