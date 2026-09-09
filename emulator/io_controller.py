"""IO controller for the machine. This is a channel-based bus that allows
the CPU to communicate with various peripherals (HDD, sound, keyboard, etc.)"""
import logging

from .memory_map import IO_SIZE, IO_START, IOHeader

log = logging.getLogger(__name__)

_DATA_BASE = IO_START + IOHeader.USABLE_AFTER
# The data window ends exactly where the framebuffer begins, so an
# over-long device reply would scribble into display memory.
_DATA_WINDOW = IO_SIZE - IOHeader.USABLE_AFTER

# Written to RETURN_DATA when a guest selects a channel with no device on it.
ERR_NO_SUCH_CHANNEL = 0xFFFFFFFF


class IOChannel:
    """A single channel on the IO bus. Each channel has a callback that is called
    when the CPU issues a command to that channel. The callback is responsible for
    reading/writing data from/to RAM as needed."""
    def __init__(self, callback, name=None):
        self.callback = callback
        self.name = name


class IOController:
    def __init__(self, ram):
        self.ram = ram
        self.channels = {}

    def register_channel(self, channel_id, channel: IOChannel):
        self.channels[channel_id] = channel

    def update(self):
        """Run a pending command, if the guest has selected a channel.

        Called when ram.io_pending says a write landed in the IO window.
        This method writes to RAM itself (clearing the channel, storing the
        return length), which would re-arm that flag -- so it clears the
        flag last, after all of its own writes.
        """
        ram = self.ram
        channel_id = ram.read_word(IO_START + IOHeader.IO_CHANNEL)
        if channel_id == 0:  # no channel selected
            ram.io_pending = False
            return

        channel = self.channels.get(channel_id)
        if channel is None:
            # A guest typo used to surface as a bare KeyError traceback with
            # no PC and no context. Report it in-band and keep running --
            # real hardware doesn't halt when you address an empty slot.
            log.warning("IO: no device on channel %d", channel_id)
            ram.write_word(IO_START + IOHeader.RETURN_DATA, ERR_NO_SUCH_CHANNEL)
            ram.write_word(IO_START + IOHeader.IO_CHANNEL, 0)
            ram.io_pending = False
            return

        read_write = ram.read_word(IO_START + IOHeader.IO_R_W)
        command = ram.read_word(IO_START + IOHeader.COMMAND)
        length = ram.read_word(IO_START + IOHeader.LENGTH)
        address = ram.read_word(IO_START + IOHeader.ADDRESS)

        # One slice instead of length//4 read_word calls. The old loop also
        # dropped the last 1-3 bytes of any transfer that wasn't a multiple
        # of 4; this doesn't.
        data = bytearray(ram.mem[_DATA_BASE:_DATA_BASE + length]) if read_write == 1 \
            else bytearray(length)

        io_data = channel.callback(read_write, command, length, address, data)
        io_data = b"" if io_data is None else io_data  # devices may return None

        log.debug("IO: channel %d (%s) cmd=%d len=%d addr=%#x rw=%d -> %d bytes",
                  channel_id, channel.name, command, length, address, read_write, len(io_data))

        if len(io_data) > _DATA_WINDOW:
            log.warning("IO: channel %d returned %d bytes, window is %d -- truncating",
                        channel_id, len(io_data), _DATA_WINDOW)
            io_data = io_data[:_DATA_WINDOW]

        if read_write == 0 and io_data:
            ram.mem[_DATA_BASE:_DATA_BASE + len(io_data)] = io_data

        ram.write_word(IO_START + IOHeader.RETURN_DATA, len(io_data))
        ram.write_word(IO_START + IOHeader.IO_CHANNEL, 0)  # clear channel after processing
        ram.io_pending = False  # last: our own writes above re-armed it
