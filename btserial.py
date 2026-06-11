"""macOS Bluetooth RFCOMM transport with a pyserial-compatible interface.

The Brother PT-P300BT is a Bluetooth Classic (SPP) device. On macOS the
``/dev/cu.PT-P300BT*`` serial port only carries data while the connection set
up by the GUI pairing is still live; once the printer auto-sleeps, reopening the
port (or reconnecting with blueutil) brings up the base link but NOT a working
data session. Opening an RFCOMM channel directly through the IOBluetooth
framework performs the same full connect the GUI does and works even from a
cold/slept state, so it lets us print from the command line with no GUI step --
the printer only has to be paired with the Mac once.

This module exposes :class:`RFCOMMSerial`, a small adapter that mimics the parts
of ``serial.Serial`` used by labelmaker/printlabel (``write``, ``read``,
``reset_input_buffer``, ``flush``, ``close`` and a ``timeout`` attribute), so the
existing print code can use it unchanged.
"""

import re
import time

import objc
from Foundation import NSObject, NSRunLoop, NSDate
from IOBluetooth import IOBluetoothDevice, IOBluetoothSDPUUID

# Serial Port Profile service class UUID (Bluetooth assigned number 0x1101).
_SPP_UUID16 = 0x1101

_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def looks_like_bt_address(value):
    """True if value is a Bluetooth MAC address (colon or dash separated)."""
    return bool(_MAC_RE.match(value or ''))


class _ChannelDelegate(NSObject):
    """Collects RFCOMM open status and incoming data via IOBluetooth callbacks."""

    def init(self):
        self = objc.super(_ChannelDelegate, self).init()
        self.buf = b''
        self.open_status = None
        self.closed = False
        return self

    def rfcommChannelOpenComplete_status_(self, channel, status):
        self.open_status = int(status)

    def rfcommChannelData_data_length_(self, channel, data, length):
        self.buf += bytes(data[:length])

    def rfcommChannelClosed_(self, channel):
        self.closed = True


def _pump(seconds):
    """Run the current run loop for ``seconds`` so IOBluetooth callbacks fire."""
    rl = NSRunLoop.currentRunLoop()
    end = time.time() + seconds
    while time.time() < end:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))


class RFCOMMSerial:
    """Minimal pyserial-compatible wrapper over an IOBluetooth RFCOMM channel."""

    def __init__(self, address, timeout=10, open_timeout=15):
        self.timeout = timeout
        self._delegate = _ChannelDelegate.alloc().init()

        device = IOBluetoothDevice.deviceWithAddressString_(address)
        if device is None:
            raise IOError(f'No paired Bluetooth device with address {address!r}. '
                          'Pair the printer with macOS once via System Settings.')
        self._device = device

        spp = IOBluetoothSDPUUID.uuid16_(_SPP_UUID16)
        record = device.getServiceRecordForUUID_(spp)
        if record is None:
            raise IOError(f'Device {device.nameOrAddress()} exposes no Serial Port '
                          '(SPP) service. Is it paired and powered on?')
        ok, channel_id = record.getRFCOMMChannelID_(None)
        if ok != 0:
            raise IOError('Could not read RFCOMM channel id from the SDP record.')

        result, channel = device.openRFCOMMChannelAsync_withChannelID_delegate_(
            None, channel_id, self._delegate)
        if result != 0 or channel is None:
            raise IOError(f'Failed to start RFCOMM channel (IOReturn {result}). '
                          'Make sure the printer is on and not connected elsewhere.')
        self._channel = channel

        deadline = time.time() + open_timeout
        while self._delegate.open_status is None and time.time() < deadline:
            _pump(0.1)
        if self._delegate.open_status is None:
            raise IOError(f'Timed out after {open_timeout}s opening the RFCOMM '
                          'channel — the printer may be asleep or out of range.')
        if self._delegate.open_status != 0:
            raise IOError('Printer refused the RFCOMM connection '
                          f'(status {self._delegate.open_status}).')

    def write(self, data):
        data = bytes(data)
        # writeSync blocks until the data has been sent.
        self._channel.writeSync_length_(data, len(data))
        return len(data)

    def read(self, size=1):
        """Return up to ``size`` bytes, waiting up to ``self.timeout`` seconds."""
        deadline = time.time() + (self.timeout if self.timeout is not None else 0)
        while len(self._delegate.buf) < size:
            if self.timeout is not None and time.time() >= deadline:
                break
            if self._delegate.closed:
                break
            _pump(0.1)
        out, self._delegate.buf = self._delegate.buf[:size], self._delegate.buf[size:]
        return out

    def reset_input_buffer(self):
        _pump(0.05)
        self._delegate.buf = b''

    def flush(self):
        _pump(0.02)

    def close(self):
        try:
            self._channel.closeChannel()
        except Exception:
            pass
