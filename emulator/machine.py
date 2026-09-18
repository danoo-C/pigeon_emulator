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
from pathlib import Path
from typing import Optional

from .bios import BIOS
from .cpu import _HANDLERS, _UNPACK, CPU
from .instruction_set import INSTR_SIZE
from .devices.cd import CD
from .devices.debug_port import DebugPort
from .devices.display_io import DisplayIO
from .devices.gac import GAC
from .devices.hdd import HDD
from .devices.hid import HID
from .devices.timer import Timer
from .devices.vram import VRAM
from .io_controller import IOChannel, IOController
from .memory_map import (
    BIOS2_MAX, CH_BIOS2, CH_CD, CH_DEBUG, CH_DISPLAY, CH_HDD, CH_HID, CH_TIMER, CH_USERPROG,
    CH_GAC, CH_VRAM, DISPLAY_H, DISPLAY_MODES, DISPLAY_W, RAM_SIZE, REGISTER_COUNT, VEC_BREAK,
    VEC_TIMER, VRAM_SIZE,
)
from .ram import RAM

log = logging.getLogger(__name__)

DISPLAY_FPS = 30

# Instructions between clock reads. Small enough that a 30 FPS frame
# deadline is never overshot (at ~2.4M IPS this is ~4 ms), large enough
# that time.time() costs nothing measurable per instruction. The devices
# are asked for interrupts as often, so a timer tick or a Ctrl+C waits at
# most this many instructions.
CLOCK_SAMPLE_INTERVAL = 10_000


class Machine:
    """A wired-up pigeon computer, ready to run."""

    def __init__(self, bios_path="build/bios.bin", program_path=None,
                 disk_path=None, ram_size=RAM_SIZE, cd=None, bios2_path=None,
                 vram_size=VRAM_SIZE, display_mode=(DISPLAY_W, DISPLAY_H),
                 display_modes=DISPLAY_MODES):
        # Video memory is mapped above RAM whatever its size; 0 is the
        # machine from before it existed (docs/gac/phase1_aperture.md).
        self.ram = RAM(ram_size, vram_size)
        self.cpu = CPU(self.ram, REGISTER_COUNT)
        self.io_controller = IOController(self.ram)

        # Channel 1: the disk the BIOS boots the user program from. Omitting
        # a program leaves the channel unregistered rather than opening the
        # current directory as a disk image, which is what an empty path
        # used to do (IsADirectoryError, from the "leave blank to skip" prompt).
        self.user_prog: Optional[HDD] = None
        if program_path:
            self.user_prog = HDD(program_path, ram=self.ram)
            self.io_controller.register_channel(
                CH_USERPROG, IOChannel(self.user_prog.callback, name="USERPROG"))
        elif not bios2_path:
            # With a second stage, an empty channel 1 is ordinary: bios2
            # decides what boots.
            log.warning("No user program: channel %d is empty, the BIOS will "
                        "boot into whatever is at the load address", CH_USERPROG)

        self.hdd = HDD(disk_path, ram=self.ram)
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
        # The debug port, always there as the CD drive is: a program asks
        # it once whether it exists, and <pigeon/debug.h> answers -1 at
        # once on a machine without it (docs/phase5_plan.md §4.1).
        self.debug = DebugPort(self.ram)
        # Channel 9: video memory's control side -- the mode, the surfaces,
        # what the screen shows (docs/gac/phase2_vram.md). Only with video
        # memory; without it the channel is empty, VRAM's INFO answers
        # 0xFFFFFFFF instead of its magic, and the screen stays 192 x 108.
        # Channel 10: the accelerator, which draws on VRAM's surfaces, so it
        # comes and goes with them (docs/gac/plans/phase3_gac.md, Q5).
        self.vram: Optional[VRAM] = None
        self.gac: Optional[GAC] = None
        if self.ram.vram_size:
            self.vram = VRAM(self.ram, self.display_io, display_modes, display_mode)
            self.gac = GAC(self.ram, self.vram)
            self.io_controller.register_channel(
                CH_VRAM, IOChannel(self.vram.callback, name="VRAM"))
            self.io_controller.register_channel(
                CH_GAC, IOChannel(self.gac.callback, name="GAC"))
        elif tuple(display_mode) != (DISPLAY_W, DISPLAY_H):
            raise ValueError(f"a {display_mode[0]}x{display_mode[1]} screen needs video "
                             f"memory; without it the screen is {DISPLAY_W}x{DISPLAY_H}")
        for channel_id, device, name in (
            (CH_HDD, self.hdd, "HDD"),
            (CH_HID, self.hid, "HID"),
            (CH_TIMER, self.timer, "TIMER"),
            (CH_DISPLAY, self.display_io, "DISPLAY"),
            (CH_CD, self.cd, "CD"),
            (CH_DEBUG, self.debug, "DEBUG"),
        ):
            self.io_controller.register_channel(
                channel_id, IOChannel(device.callback, name=name))

        # Channel 7: the firmware device the BIOS loads its second stage
        # from (docs/os_cd.md). Read-only, never created, and -- like
        # channel 1 -- registered only when there is something to put on
        # it. Without it the channel is empty and the BIOS boots channel 1
        # as it always has, so Machine(bios2_path=None) is the machine from
        # before the second stage existed.
        self.bios2: Optional[HDD] = None
        if bios2_path:
            try:
                self.bios2 = HDD(bios2_path, ram=self.ram, readonly=True)
            except OSError:
                self.close()
                raise
            if self.bios2.size > BIOS2_MAX:
                size = self.bios2.size
                self.close()
                raise ValueError(f"second-stage BIOS too large ({size} bytes), "
                                 f"max is {BIOS2_MAX}")
            self.io_controller.register_channel(
                CH_BIOS2, IOChannel(self.bios2.callback, name="BIOS2"))

        self.bios = BIOS.from_file(bios_path)
        self.bios.write_bios(self.ram)

        self.instruction_count = 0
        self.total_instructions = 0
        self.last_ips = 0.0
        # Instructions until the devices are next asked for interrupts. One
        # countdown for step() and run() both, so a program takes its
        # interrupts at the same instructions whichever of the two runs it.
        self._countdown = CLOCK_SAMPLE_INTERVAL

    # --- lifecycle --------------------------------------------------------

    def start_servers(self, host="127.0.0.1", display_port=8000, hid_port=8001,
                      cd_port=8002):
        """Bind the display, input and CD HTTP servers. Separate from
        __init__ so a test can build a Machine without touching the
        network."""
        self.display_io.hid_url = f"http://{host}:{hid_port}"
        self.display_io.cd_url = f"http://{host}:{cd_port}"
        self.display_io.debug_port = self.debug
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
        for disk in (self.user_prog, self.hdd, self.cd, self.bios2):
            if disk is not None:
                disk.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- execution --------------------------------------------------------

    def step(self):
        """Execute one instruction, taking a pending interrupt first, and
        service any IO it triggered.

        Returns 1 once the CPU has halted, else 0.
        """
        if self.cpu.run() == 1:
            return 1
        if self.ram.io_pending:
            self.io_controller.update()
        self._countdown -= 1
        if not self._countdown:
            self._countdown = CLOCK_SAMPLE_INTERVAL
            self.poll_devices()
        return 0

    def poll_devices(self):
        """Turn what the devices raised into pending interrupts: a ticking
        timer come due, and Ctrl+C with break on. step() and run() call this
        every CLOCK_SAMPLE_INTERVAL instructions, and the CPU takes the
        interrupt before its next instruction with interrupts on."""
        if self.timer.poll():
            self.cpu.interrupt(VEC_TIMER)
        if self.hid.take_break():
            self.cpu.interrupt(VEC_BREAK)

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
        # Carried on from where step() or the last run() left it, and the
        # window it counts down is how many instructions the next sample
        # point adds to the totals.
        countdown = window = self._countdown

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

        try:
            while True:
                if cpu.halted:
                    break
                # Before every instruction, as CPU.run() does (docs/kernel.md Q5).
                if cpu.pending and cpu.ie:
                    cpu.take_interrupt()

                pc = cpu.pc
                # A try costs nothing until it catches, on Python 3.11 and up.
                try:
                    opcode, dst, src1, src2, imm = unpack(memory, pc)
                except struct.error:
                    cpu.fetch_fault(pc)
                else:
                    cpu.pc = pc + instruction_size
                    # An opcode with no instruction has the bad-opcode fault here.
                    handlers[opcode](cpu, dst, src1, src2, imm)

                if ram.io_pending:
                    controller.update()

                # Reading the clock costs more than executing an instruction, so
                # do it once per CLOCK_SAMPLE_INTERVAL rather than twice per
                # instruction. A countdown beats a modulo on this path.
                countdown -= 1
                if countdown:
                    continue
                executed += window
                self.instruction_count += window
                self.total_instructions += window
                window = countdown = CLOCK_SAMPLE_INTERVAL
                self.poll_devices()

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
        finally:
            # Count the partial window the loop ended in, so the totals are
            # exact rather than rounded down to the last sample point.
            remainder = window - countdown
            self.instruction_count += remainder
            self.total_instructions += remainder
            self._countdown = countdown

    def dump_ram_to(self, path):
        """Write RAM out for debugging, and video memory beside it as
        <name>_vram<suffix> when there is any. Returns the paths written."""
        path = Path(path)
        with open(path, "wb") as f:
            f.write(self.ram.dump_ram())
        written = [path]
        if self.ram.vram_size:
            vram_path = path.with_name(f"{path.stem}_vram{path.suffix}")
            with open(vram_path, "wb") as f:
                f.write(self.ram.vram)
            written.append(vram_path)
        return written
