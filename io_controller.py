"""IO controller for the machine. This is a channel-based bus that allows
the CPU to communicate with various peripherals (HDD, sound, keyboard, etc.)"""
from ram import RAM
from memory_map import IO_START, IO_SIZE, IOHeader
import ram
class IOChannel:
    """A single channel on the IO bus. Each channel has a callback that is called
    when the CPU issues a command to that channel. The callback is responsible for
    reading/writing data from/to RAM as needed."""
    def __init__(self,  callback, name=None):
        self.callback = callback
        self.name = name


class IOController:
    def __init__(self, ram):
        self.ram = ram
        self.channels = {}

    def register_channel(self, channel_id, channel:IOChannel):
        self.channels[channel_id] = channel

    def update(self):
        """Called once per CPU cycle to update any stateful devices."""
        if self.ram.read_word(IO_START + IOHeader.IO_CHANNEL) == 0:  # no channel selected
            return
        # if self.ram.read_word(IO_START + IOHeader.COMMAND) == 0:  # no command issued
        #     return
        channel_id = self.ram.read_word(IO_START + IOHeader.IO_CHANNEL)
        read_write = self.ram.read_word(IO_START + IOHeader.IO_R_W)
        command = self.ram.read_word(IO_START + IOHeader.COMMAND)
        length = self.ram.read_word(IO_START + IOHeader.LENGTH)
        address = self.ram.read_word(IO_START + IOHeader.ADDRESS)
        data = bytearray(length)

        if read_write == 1:  # write
            for i in range(length//4):
                data[i*4:(i+1)*4] = self.ram.read_word(IO_START + IOHeader.USABLE_AFTER + i*4).to_bytes(4, byteorder="little")
       
        io_data = self.channels[channel_id].callback(read_write, command, length, address, data)
        if(channel_id in [1,2]):
            print(f"IOController: channel {channel_id} command {command} length {length} address {address} read_write {read_write} returned {len(io_data)} bytes")
        
        if read_write == 0 and io_data != None:  # read
            for i in range(len(io_data)//4):
                self.ram.write_word(IO_START + IOHeader.USABLE_AFTER + i*4, int.from_bytes(io_data[i*4:(i+1)*4], byteorder="little"))
        self.ram.write_word(IO_START + IOHeader.IO_CHANNEL, 0)  # clear channel after processing
        # self.ram.write_word(IO_START + IOHeader.COMMAND, 0)  # clear command after processing
        # self.ram.write_word(IO_START + IOHeader.LENGTH, 0)  # clear length after processing
        self.ram.write_word(IO_START + IOHeader.RETURN_DATA, len(io_data))  # clear return data after processing
