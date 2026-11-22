"""MPEG-TS handling for the in-memory pre-record buffer.

The camera's main stream is copied (not re-encoded) by ffmpeg into MPEG-TS and read
here in 188-byte packets. We keep the last few seconds in memory, grouped by keyframe,
so a clip can start a few seconds before the motion without touching the disk.
"""
import collections
import time

PACKET = 188
SYNC = 0x47
VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24}  # MPEG-1/2, MPEG-4, H.264, H.265


class TsPacketizer:
    """Splits a byte stream into whole TS packets, re-syncing if bytes are lost."""

    def __init__(self):
        self._rest = b""

    def feed(self, data):
        buf = self._rest + data
        i = 0
        n = len(buf)
        while i + PACKET <= n:
            if buf[i] != SYNC:
                j = buf.find(bytes([SYNC]), i + 1)
                if j < 0:
                    i = n
                    break
                i = j
                continue
            yield buf[i:i + PACKET]
            i += PACKET
        self._rest = buf[i:]


def _pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def _pusi(pkt):
    return bool(pkt[1] & 0x40)


def _payload(pkt):
    afc = (pkt[3] >> 4) & 0x3
    if afc in (0, 2):
        return b""
    start = 4
    if afc == 3:
        start = 5 + pkt[4]
    return pkt[start:]


def _random_access(pkt):
    afc = (pkt[3] >> 4) & 0x3
    if afc not in (2, 3) or pkt[4] == 0:
        return False
    return bool(pkt[5] & 0x40)


def _section(pkt):
    """Returns the PSI section from a packet that starts one."""
    pl = _payload(pkt)
    if not pl:
        return b""
    pointer = pl[0]
    return pl[1 + pointer:]


class TsStream:
    """Tracks PAT/PMT and spots where each video keyframe starts."""

    def __init__(self):
        self.pat = None
        self.pmt = None
        self.pmt_pid = None
        self.video_pid = None
        self.video_type = None

    def inspect(self, pkt):
        """Returns True if this packet starts a video keyframe."""
        pid = _pid(pkt)
        if pid == 0 and _pusi(pkt):
            sec = _section(pkt)
            if len(sec) >= 16 and sec[0] == 0x00:
                self.pat = pkt
                # First programme entry after the 8-byte header.
                body = sec[8:]
                section_len = ((sec[1] & 0x0F) << 8) | sec[2]
                end = min(len(sec), 3 + section_len - 4) - 8
                k = 0
                while k + 4 <= end:
                    prog = (body[k] << 8) | body[k + 1]
                    p = ((body[k + 2] & 0x1F) << 8) | body[k + 3]
                    if prog != 0:
                        self.pmt_pid = p
                        break
                    k += 4
            return False
        if self.pmt_pid is not None and pid == self.pmt_pid and _pusi(pkt):
            sec = _section(pkt)
            if len(sec) >= 12 and sec[0] == 0x02:
                self.pmt = pkt
                section_len = ((sec[1] & 0x0F) << 8) | sec[2]
                prog_info_len = ((sec[10] & 0x0F) << 8) | sec[11]
                k = 12 + prog_info_len
                end = min(len(sec), 3 + section_len - 4)
                while k + 5 <= end:
                    st = sec[k]
                    epid = ((sec[k + 1] & 0x1F) << 8) | sec[k + 2]
                    es_len = ((sec[k + 3] & 0x0F) << 8) | sec[k + 4]
                    if st in VIDEO_STREAM_TYPES and self.video_pid is None:
                        self.video_pid = epid
                        self.video_type = st
                    k += 5 + es_len
            return False
        if self.video_pid is not None and pid == self.video_pid and _pusi(pkt):
            return _random_access(pkt)
        return False

    def headers(self):
        return (self.pat or b"") + (self.pmt or b"")


class PreRollBuffer:
    """Keeps the last `seconds` of stream in memory, starting on a keyframe."""

    MAX_BYTES = 96 * 1024 * 1024  # safety cap

    def __init__(self, seconds):
        self.seconds = seconds
        self.gops = collections.deque()  # [start_time, bytearray]
        self.size = 0

    def add(self, pkt, keyframe, now=None):
        now = time.monotonic() if now is None else now
        if keyframe or not self.gops:
            if not keyframe and not self.gops:
                return  # wait for the first keyframe
            self.gops.append([now, bytearray()])
        self.gops[-1][1] += pkt
        self.size += len(pkt)
        # Drop whole keyframe groups once the next group starts early enough.
        cutoff = now - self.seconds
        while len(self.gops) > 1 and self.gops[1][0] <= cutoff:
            self.size -= len(self.gops.popleft()[1])
        while len(self.gops) > 1 and self.size > self.MAX_BYTES:
            self.size -= len(self.gops.popleft()[1])

    def snapshot(self):
        return b"".join(bytes(g[1]) for g in self.gops)

    def clear(self):
        self.gops.clear()
        self.size = 0
