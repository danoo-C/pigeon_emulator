# ram.py
from memory_map import (
    RAM_SIZE, IO_START, IO_SIZE, DISPLAY_START, DISPLAY_SIZE,
)


class RAM:
    def __init__(self, size=RAM_SIZE):
        self.size = size
        self.mask = size - 1  # size is a power of two, so this wraps addresses cleanly
        self.mem = bytearray(size)
        self.io = None  # an IOController, attached via attach_io()

    def attach_io(self, io_controller):#unused
        self.io = io_controller
        io_controller.attach_ram(self)  # gives the controller DMA access to RAM

    def read_byte(self, addr):
        addr &= self.mask
        if self.io is not None and IO_START <= addr < IO_START + IO_SIZE:
            return self.io.read(addr - IO_START)
        return self.mem[addr]

    def write_byte(self, addr, value):
        addr &= self.mask
        if self.io is not None and IO_START <= addr < IO_START + IO_SIZE:
            self.io.write(addr - IO_START, value)
            return
        self.mem[addr] = value & 0xFF

    def read_word(self, addr):
        a = addr & self.mask
        return int.from_bytes(self.mem[a:a + 4], byteorder="little", signed=False)

    def write_word(self, addr, value):
        a = addr & self.mask
        value &= 0xFFFFFFFF
        self.mem[a:a + 4] = value.to_bytes(4, byteorder="little", signed=False)

    def load_bytes(self, data, start=0):
        end = start + len(data)
        if end > self.size:
            raise ValueError("Data too large to fit in RAM at that address")
        self.mem[start:end] = data

    def display_slice(self):
        return self.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE]
    
    def dump_ram(self, start=0, end=None):
        if end is None:
            end = self.size
        return self.mem[start:end]