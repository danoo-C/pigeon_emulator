#!/usr/bin/env python3
"""Entry point for the pigeon emulator.

Creates the main components (RAM, CPU), optionally loads a program, and
runs a simple mainloop that executes instructions until `HALT`.
"""
import argparse
import sys
import time
from typing import IO

import cpu
from ram import RAM
from cpu import CPU
from memory_map import PROGRAM_LOAD_ADDR, RAM_SIZE
from instruction_set import encode, NONE_REG
from io_controller import IOController, IOChannel
from hdd import HDD
from bios import BIOS
from display_io import DisplayIO
from HID import HID
from io_timer import Timer
from instruction_set import INSTRUCTIONS_BY_OPCODE, NONE_REG
DISPLAY_DUMP_INTERVAL = 10000
DISPLAY_FPS = 30

DEMO_PROGRAM = None

class PigeonEmulator:
    def __init__(self, bios_path: str = "bios.bin"):
        self.ram = RAM(RAM_SIZE)
        self.cpu = CPU(self.ram, 6)
        self.io_controller = IOController(self.ram)
        
        #the user program is loaded into this hdd on io chanel 1. the bios will load from here.
        userprog = input("Enter path to user program (or leave blank to skip): ").strip()


        self.user_prog = HDD(userprog)
        self.io_controller.register_channel(1, IOChannel(self.user_prog.callback, name="USERPROG"))

        self.hdd = HDD()
        self.io_controller.register_channel(2, IOChannel(self.hdd.callback, name="HDD"))

    
        self.hid = HID()
        self.io_controller.register_channel(3, IOChannel(self.hid.callback, "HID"))

        self.timer = Timer()
        self.io_controller.register_channel(4, IOChannel(self.timer.callback, "TIMER"))

        self.bios = BIOS.from_file(bios_path)

        self.display_io = DisplayIO(self.ram)
        self.dump_frame_counter = 0

        self.bios.write_bios(self.ram)

        self.instruction_count  = 0
        self.last_ips_time = time.time()

        self.last_disp_time = time.time()

        self.display_io.start_fastapi(host="127.0.0.1", port=8000)
        self.hid.start_fastapi(host="127.0.0.1", port=8001)
        # DEBUG: verify BIOS was loaded correctly
        print("\n=== DEBUG: Checking BIOS in RAM ===")
        print(f"BIOS size: {len(self.bios.bios_bytes)} bytes")
        print(f"Initial PC: {self.cpu.pc:#06x}")
        
        # Check instruction at 0x0078
        chunk = bytes(self.ram.mem[0x0078:0x0080])
        print(f"RAM at 0x0078: {' '.join(f'{b:02x}' for b in chunk)}")
        print(f"  Opcode: {chunk[0]} (should be 9 for MRW)")
        
        # Check first instruction
        chunk0 = bytes(self.ram.mem[0x0000:0x0008])
        print(f"RAM at 0x0000: {' '.join(f'{b:02x}' for b in chunk0)}")
        print(f"  Opcode: {chunk0[0]} (should be 1 for MOV)")
        print("===\n")

        # DEBUG: dump the entire BIOS from RAM
        print("\n=== Full BIOS disassembly ===")
        from instruction_set import INSTRUCTIONS_BY_OPCODE, NONE_REG
        for i in range(len(self.bios.bios_bytes)//8):
            offset = i * 8
            chunk = bytes(self.ram.mem[offset:offset+8])
            opcode = chunk[0]
            if opcode in INSTRUCTIONS_BY_OPCODE:
                name = INSTRUCTIONS_BY_OPCODE[opcode].name
            else:
                name = f"UNKNOWN({opcode})"
            dst, src1, src2 = chunk[1], chunk[2], chunk[3]
            imm = int.from_bytes(chunk[4:8], 'little')
            def fmt(x):
                return '.' if x == 0xFF else str(x)
            print(f"0x{offset:04x}: {name:<6} dst={fmt(dst):<3} src1={fmt(src1):<3} src2={fmt(src2):<3} imm=0x{imm:08x}")
        #time.sleep(10)  # give user time to read the BIOS dump before menu appears

    def _print_dissasembly_at_pc(self, pc: int):
        offset = pc
        chunk = bytes(self.ram.mem[offset:offset+8])
        opcode = chunk[0]
        if opcode in INSTRUCTIONS_BY_OPCODE:
            name = INSTRUCTIONS_BY_OPCODE[opcode].name
        else:
            name = f"UNKNOWN({opcode})"
        dst, src1, src2 = chunk[1], chunk[2], chunk[3]
        imm = int.from_bytes(chunk[4:8], 'little')
        def fmt(x):
            return '.' if x == 0xFF else str(x)
        print(f"0x{offset:04x}: {name:<6} dst={fmt(dst):<3} src1={fmt(src1):<3} src2={fmt(src2):<3} imm=0x{imm:08x}")


    def start(self):
        self.handle_menu()

    def load_program_from_file(self, path):
        with open(path, "rb") as f:
            return f.read()

    def display_menu(self):
        print("Pigeon Emulator Menu:")
        print("----------------------------")
        print("0. continue running")
        print("1. normal run")
        print("2. enter debug mode")
        print("3. dump RAM")
        print("4. Exit")
        choice = input("Enter your choice (1-4): ")
        return choice.strip()

    def handle_menu(self):
        while True:
            choice = self.display_menu()
            if choice == "0":
                break
            elif choice == "1":
                self.run()
            elif choice == "2":
                self.run_debug()
                pass
            elif choice == "3":
                #dumps the whole ram into ram.bin for debugging purposes
                with open("ram.bin", "wb") as f:
                    f.write(self.ram.dump_ram())

            elif choice == "4":
                sys.exit(0)
            else:
                print("Invalid choice. Please try again.")
    def pc_tasks(self):
        self.io_controller.update()
        #self.dump_frame_counter += 1
        # if self.dump_frame_counter >= DISPLAY_DUMP_INTERVAL:
        #     self.dump_frame_counter = 0
        #     self.display_io.update()
    def run(self):

        try:   
            while 1:
                if self.cpu.run() == 1:
                    break
                self.pc_tasks()


                # In main loop:
                self.instruction_count += 1                    # Increment after each instruction
                current_time = time.time()
                
                elapsed = current_time - self.last_ips_time
                if(current_time - self.last_disp_time >= 1/DISPLAY_FPS):
                    self.display_io.update()
                    self.last_disp_time = current_time
                # Every 1 second:
                if elapsed >= 1.0:
                    ips = self.instruction_count / elapsed
                    print(f"[IPS] {self.instruction_count} instructions = {ips:.0f} IPS")
                    
                    # Reset for next second
                    self.instruction_count = 0
                    self.last_ips_time = current_time
                #time.sleep(0.00005)


        except KeyboardInterrupt:
            print("\nInterrupted by user")
            # On Ctrl-C, dump CPU state and exit immediately
            try:
                self.handle_menu()
            except Exception:
                pass
            sys.exit(0)
        except RuntimeError as e:
            print("PIGEON EMULATOR RAN INTO AN ERROR")
            print("==================================")
            print("dumping RAM into ram.bin")
            with open("ram.bin", "wb") as f:
                                f.write(self.ram.dump_ram())
            raise e

    def run_debug(self):
        print("Entering debug mode. press enter to step through instructions. Press Ctrl-C to interrupt and enter menu.")
        try:   
            while 1:
                if self.cpu.pc < 0x10000:
                    if self.cpu.run() == 1:
                        break
                    self.pc_tasks()
                    continue
                input("")
                if self.cpu.run() == 1:
                    break
                self._print_dissasembly_at_pc(self.cpu.pc)
                print(self.cpu.dump())
                self.pc_tasks()

        except KeyboardInterrupt:
            print("\nInterrupted by user")
            # On Ctrl-C, dump CPU state and exit immediately
            try:
                self.handle_menu()
            except Exception:
                pass
            sys.exit(0)


def main(argv=None):
    x = PigeonEmulator()
    x.start()


if __name__ == "__main__":
    main()
