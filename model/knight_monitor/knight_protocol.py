"""Knight Board wire protocol: framing, raw counts, and command strings.

Deliberately free of Qt and of the serial port, so it can be exercised by
feeding it bytes.  Frame layouts come from the NeuroPawn data-format docs:

    EEG-only  (21 bytes)
        [0]      0xA0
        [1]      rolling frame counter, 0-255
        [2..17]  8 x int16, big endian, raw ADC counts
        [18]     LOFF P
        [19]     LOFF N
        [20]     0xC0

    IMU       (57 bytes)
        [0]      0xA0
        [1]      rolling frame counter, 0-255
        [2..17]  8 x int16, big endian, raw ADC counts
        [18]     LOFF P
        [19]     LOFF N
        [20..31] accelerometer X Y Z, float32 little endian
        [32..43] gyroscope X Y Z
        [44..55] magnetometer X Y Z
        [56]     0xC0
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

START_BYTE = 0xA0
END_BYTE = 0xC0

BAUD = 115200
SAMPLE_RATE = 125
N_CHANNELS = 8

# Fixed analog front-end gain, used by the datasheet's uV/count formula.
AFE_GAIN = 79.57
GAINS = (1, 2, 3, 4, 6, 8, 12)
DEFAULT_GAIN = 12

FRAME_EEG = 21
FRAME_IMU = 57

IMU_AXES = ("ax", "ay", "az", "gx", "gy", "gz", "mx", "my", "mz")


def uv_per_count(gain: int) -> float:
    """Microvolts represented by one raw ADC count at the given gain."""
    return 4.0 / (2 ** 15 - 1) / AFE_GAIN / gain * 1_000_000.0


def full_scale_uv(gain: int) -> float:
    """+- input range in uV at the given gain."""
    return uv_per_count(gain) * 32767.0


@dataclass(frozen=True)
class Frame:
    counter: int
    counts: Tuple[int, ...]          # 8 raw int16 counts
    loff: Tuple[int, int]            # lead-off status bytes
    imu: Optional[Tuple[float, ...]] # 9 floats, or None on an EEG-only board


def _parse(buf: bytearray, i: int, n: int) -> Optional[Frame]:
    if i + n > len(buf):
        return None
    if buf[i] != START_BYTE or buf[i + n - 1] != END_BYTE:
        return None
    counter = buf[i + 1]
    counts = struct.unpack_from(">8h", buf, i + 2)
    loff = (buf[i + 18], buf[i + 19])
    imu = struct.unpack_from("<9f", buf, i + 20) if n == FRAME_IMU else None
    return Frame(counter, counts, loff, imu)


class FrameParser:
    """Byte-stream to Frame decoder with start/end marker resynchronisation.

    mode is "auto", "eeg" or "imu".  In auto mode it locks on to whichever
    frame length yields three consecutive frames with incrementing counters,
    which is enough to distinguish a 21-byte stream from a 57-byte one even
    though both start with 0xA0 and end with 0xC0.
    """

    def __init__(self, mode: str = "auto") -> None:
        if mode not in ("auto", "eeg", "imu"):
            raise ValueError("mode must be auto, eeg or imu")
        self.mode = mode
        self.buf = bytearray()
        self.dropped = 0
        self._locked = mode in ("eeg", "imu")

    @property
    def frame_length(self) -> Optional[int]:
        if self.mode == "eeg":
            return FRAME_EEG
        if self.mode == "imu":
            return FRAME_IMU
        return None

    def _sniff(self) -> Optional[int]:
        """Pick a frame length, or None if more bytes are needed."""
        for n in (FRAME_IMU, FRAME_EEG):
            if len(self.buf) < n * 3:
                continue
            ok = True
            prev = None
            for k in range(3):
                fr = _parse(self.buf, k * n, n)
                if fr is None:
                    ok = False
                    break
                if prev is not None and (fr.counter - prev) & 0xFF != 1:
                    ok = False
                    break
                prev = fr.counter
            if ok:
                return n
        # enough data and nothing fits: drop one byte so we can slide forward
        if len(self.buf) >= FRAME_IMU * 3:
            return -1
        return None

    def feed(self, data: bytes) -> List[Frame]:
        self.buf.extend(data)
        frames: List[Frame] = []

        while True:
            i = self.buf.find(bytes([START_BYTE]))
            if i < 0:
                if len(self.buf) > 1:
                    self.dropped += len(self.buf) - 1
                    del self.buf[:-1]
                break
            if i:
                self.dropped += i
                del self.buf[:i]

            if not self._locked:
                n = self._sniff()
                if n is None:
                    break
                if n == -1:
                    self.dropped += 1
                    del self.buf[:1]
                    continue
                self.mode = "imu" if n == FRAME_IMU else "eeg"
                self._locked = True

            n = self.frame_length
            if len(self.buf) < n:
                break
            fr = _parse(self.buf, 0, n)
            if fr is None:
                self.dropped += 1
                del self.buf[:1]
                continue
            del self.buf[:n]
            frames.append(fr)

        return frames


# --- board commands -------------------------------------------------------

def cmd_channel_on(channel: int, gain: int) -> str:
    return f"chon_{channel}_{gain}"


def cmd_channel_off(channel: int) -> str:
    return f"choff_{channel}"


def cmd_rld_on(channel: int) -> str:
    return f"rldadd_{channel}"


def cmd_rld_off(channel: int) -> str:
    return f"rldremove_{channel}"


def counter_delta(previous: int, current: int) -> int:
    """Signed distance between two rolling 0-255 counters."""
    d = (current - previous) & 0xFF
    return d - 256 if d > 128 else d
