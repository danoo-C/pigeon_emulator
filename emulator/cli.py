"""Command-line front end: settings, the program picker, the menu, the stepper.

Everything interactive lives here so that machine.py stays importable and
side-effect free.
"""
import argparse
import logging
import sys
from pathlib import Path

from . import programs as programs_mod
from .config import DEFAULTS, Config, ConfigError, load_config
from .devices.cd import CD
from .instruction_set import INSTR_SIZE, disassemble, disassemble_range
from .machine import Machine
from .memory_map import PROGRAM_LOAD_ADDR
from .programs import Program, short

log = logging.getLogger(__name__)

STATUS_MARK = {"built": "*", "stale": "~", "not built": " ", "prebuilt": "+"}


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def build_bios_if_stale(config: Config, force: bool = False) -> bool:
    """Assemble the BIOS when the binary is missing or older than its source.

    build/ is gitignored, so a fresh clone has no bios.bin at all; without
    this the emulator would refuse to start until you knew to run the
    assembler by hand.
    """
    source, output = config.bios_source, config.bios_binary
    if not source.exists():
        return False
    if not force and output.exists() and output.stat().st_mtime >= source.stat().st_mtime:
        return False

    from assembler.assembler import assemble_file

    assemble_file(source, output)
    return True


# --------------------------------------------------------------------------
# The program picker
# --------------------------------------------------------------------------

def format_listing(config: Config, found=None) -> str:
    """The numbered program table."""
    found = programs_mod.discover(config) if found is None else found
    folders = ", ".join(short(d) for d in config.program_dirs)

    if not found:
        return (f"No programs found in {folders}.\n"
                f"Put a .asm or .bin there, or point \"program_dirs\" in "
                f"config.json somewhere else.")

    width = max(len(p.name) for p in found)
    lines = [f"Programs in {folders}:", ""]
    for index, program in enumerate(found, 1):
        where = short(program.source) if program.source else short(program.binary)
        lines.append(f"  {index:>2}. {STATUS_MARK[program.status]} "
                     f"{program.name:<{width}}   {where:<26} {program.status}")
    lines += ["", "  * built   ~ source is newer than the build   + binary only"]
    return "\n".join(lines)


def choose_program(config: Config) -> Program | None:
    """Show the listing and read a choice. None means 'no program'."""
    found = programs_mod.discover(config)
    missing = getattr(found[0], "missing_dirs", []) if found else []
    for folder in missing:
        print(f"warning: no such folder: {short(folder)} (from config.json)")

    print()
    print(format_listing(config, found))
    print()
    if not found:
        return None

    while True:
        try:
            choice = input("Program (number or name, Enter to skip, q to quit): ").strip()
        except EOFError:
            return None
        if not choice:
            return None
        if choice.lower() in ("q", "quit", "exit"):
            raise SystemExit(0)

        program = programs_mod.find(config, choice)
        if program is not None:
            return program
        print(f"  '{choice}' is not one of them. Pick 1-{len(found)} or a name.")


# --------------------------------------------------------------------------
# The running machine
# --------------------------------------------------------------------------

class Console:
    """The interactive menu and debugger wrapped around a Machine."""

    def __init__(self, machine: Machine, config: Config):
        self.machine = machine
        self.config = config

    MENU = """
Pigeon Emulator
----------------------------
  1. run
  2. debug (single-step)
  3. dump RAM to build/ram.bin
  4. show CPU state
  5. exit
"""

    def menu(self):
        while True:
            print(self.MENU)
            try:
                choice = input("Choice: ").strip()
            except EOFError:
                return
            if choice == "1":
                self.run()
            elif choice == "2":
                self.run_debug()
            elif choice == "3":
                path = self.machine.dump_ram_to(self.config.build_dir / "ram.bin")
                print(f"RAM written to {short(Path(path))}")
            elif choice == "4":
                print(self.machine.cpu.dump())
            elif choice == "5":
                return
            else:
                print(f"'{choice}' is not on the menu.")

    def run(self):
        try:
            self.machine.run(report_ips=lambda ips: print(f"[IPS] {ips:,.0f}"))
            print(f"\nHALT at PC={self.machine.cpu.pc:#06x} "
                  f"after {self.machine.total_instructions:,} instructions")
        except KeyboardInterrupt:
            print(f"\nInterrupted at PC={self.machine.cpu.pc:#06x}")
        except RuntimeError as e:
            dump = self.machine.dump_ram_to(self.config.build_dir / "ram.bin")
            print(f"\nEMULATOR FAULT: {e}")
            print(self.machine.cpu.dump())
            print(f"RAM dumped to {short(Path(dump))}")
            raise

    def run_debug(self):
        """Free-run through the BIOS, then step once per Enter.

        The threshold is PROGRAM_LOAD_ADDR, not a hardcoded address -- it
        used to be a literal 0x10000, left behind when the load address
        moved to 0x20000, so this silently free-ran through the gap.
        """
        print(f"Debug mode: free-running until PC >= {PROGRAM_LOAD_ADDR:#06x}, "
              f"then Enter to step. Ctrl-C for the menu.")
        machine, cpu = self.machine, self.machine.cpu
        try:
            while True:
                if cpu.pc >= PROGRAM_LOAD_ADDR:
                    print(disassemble(bytes(machine.ram.mem[cpu.pc:cpu.pc + INSTR_SIZE]), cpu.pc))
                    print("  " + cpu.dump())
                    try:
                        input("")
                    except EOFError:
                        return
                if machine.step() == 1:
                    print("HALT")
                    return
        except KeyboardInterrupt:
            print("\nInterrupted")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="start_emulator.py",
        description="Run the pigeon emulator.",
        epilog="With no --program, the configured folders are listed to pick "
               "from. Defaults come from config.json; every flag below "
               "overrides it for this run only.")

    what = parser.add_argument_group("what to run")
    what.add_argument("program", nargs="?", metavar="PROGRAM",
                      help="program to run: a name ('screen'), a number from "
                           "the listing, or a path to a .asm or .bin")
    what.add_argument("--program", "-p", dest="program_opt", metavar="PROGRAM",
                      help=argparse.SUPPRESS)  # older spelling, still accepted
    what.add_argument("--list", "-l", action="store_true",
                      help="list the available programs and exit")
    what.add_argument("--run", "-r", action="store_true",
                      help="start running immediately instead of showing the menu")

    where = parser.add_argument_group("paths (override config.json)")
    where.add_argument("--config", metavar="PATH", type=Path,
                       help="settings file (default: config.json at the repo root)")
    where.add_argument("--program-dir", metavar="DIR", action="append",
                       dest="program_dirs",
                       help="folder to scan for programs; repeatable")
    where.add_argument("--bios", metavar="PATH", dest="bios_binary")
    where.add_argument("--disk", metavar="PATH", dest="disk",
                       help="disk image for IO channel 2")
    where.add_argument("--no-autobuild", action="store_true",
                       help="never reassemble, even when a build is stale")

    net = parser.add_argument_group("servers (override config.json)")
    net.add_argument("--host")
    net.add_argument("--display-port", type=int)
    net.add_argument("--hid-port", type=int)
    net.add_argument("--cd-port", type=int,
                     help="port for the CD drive's endpoints (docs/cd-drive.md)")
    net.add_argument("--headless", action="store_true",
                     help="don't bind the display/input HTTP servers")

    dbg = parser.add_argument_group("diagnostics")
    dbg.add_argument("--disasm-bios", action="store_true",
                     help="print the BIOS disassembly at startup")
    dbg.add_argument("--verbose", "-v", action="store_true",
                     help="log every IO transaction, disk read and key event")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args.config)
    except ConfigError as e:
        parser.error(str(e))

    for key in config.unknown_keys:
        print(f"warning: {short(config.source_path)}: unknown setting "
              f"'{key}' ignored (expected one of: {', '.join(sorted(DEFAULTS))})",
              file=sys.stderr)

    config = config.override(
        host=args.host, display_port=args.display_port, hid_port=args.hid_port,
        cd_port=args.cd_port,
        program_dirs=args.program_dirs, bios_binary=args.bios_binary, disk=args.disk,
        auto_build=False if args.no_autobuild else None)

    if args.list:
        print(format_listing(config))
        return 0

    # --- pick a program -----------------------------------------------------
    wanted = args.program or args.program_opt
    program = None
    if wanted:
        as_path = Path(wanted)
        if as_path.exists() and as_path.suffix.lower() in (".asm", ".bin"):
            program = programs_mod.from_path(config, as_path)
        else:
            program = programs_mod.find(config, wanted)
            if program is None:
                print(f"No program called '{wanted}'.\n", file=sys.stderr)
                print(format_listing(config), file=sys.stderr)
                return 2
    else:
        # No isatty() gate: input() raises EOFError when stdin is empty or
        # closed (CI, </dev/null), which choose_program treats as "skip".
        # Gating on a TTY instead would make the picker unscriptable.
        program = choose_program(config)

    # --- build --------------------------------------------------------------
    config.build_dir.mkdir(parents=True, exist_ok=True)
    if config.auto_build:
        build_bios_if_stale(config)

    if not config.bios_binary.exists():
        parser.error(f"BIOS not found: {short(config.bios_binary)} "
                     f"(and {short(config.bios_source)} was not there to build it)")

    program_path = None
    if program is not None:
        try:
            program_path = program.ensure_built() if config.auto_build else program.binary
        except Exception as e:
            print(f"Could not build {program.name}: {e}", file=sys.stderr)
            return 1
        if program_path is None or not Path(program_path).exists():
            print(f"{program.name} is not built, and --no-autobuild is set.",
                  file=sys.stderr)
            return 1
        print(f"\nProgram: {program.name}  ({short(Path(program_path))})")
    else:
        print("\nNo program selected -- the BIOS will boot into an empty load address.")

    # --- run ----------------------------------------------------------------
    # The drive is built from the config here rather than inside Machine,
    # so machine.py needs to know nothing about config.json.
    drive = CD(root=config.cd_root, dirs=config.cd_dirs,
               upload_dir=config.cd_upload_dir, max_upload=config.cd_max_upload)
    machine = Machine(bios_path=str(config.bios_binary),
                      program_path=str(program_path) if program_path else None,
                      disk_path=str(config.disk), cd=drive)
    try:
        if args.disasm_bios:
            print(f"\n=== BIOS ({len(machine.bios.bios_bytes)} bytes) ===")
            for line in disassemble_range(machine.ram.mem, 0, len(machine.bios.bios_bytes)):
                print(line)
            print()

        if not args.headless:
            machine.start_servers(host=config.host,
                                  display_port=config.display_port,
                                  hid_port=config.hid_port,
                                  cd_port=config.cd_port)
            print(f"Display: {config.display_url}   HID: {config.hid_url}   "
                  f"CD: {config.cd_url}")

        console = Console(machine, config)
        if args.run:
            console.run()
        else:
            console.menu()
    finally:
        machine.close()
    return 0
