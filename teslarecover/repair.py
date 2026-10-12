"""Rebuild playable MP4s from decrypted clips whose index (moov) is missing or broken.

Tesla writes ftyp, then mdat (the H.264 frames), then moov at the very end. A deleted clip
that was fragmented on disk usually loses its end - including moov - or gets bytes of
another file spliced in. The frames before the break are still fine, so this walks the
length-prefixed NAL units in mdat, groups them into frames (each frame starts with an SEI
or SPS/PPS before its slice), and stops at the first thing that isn't a valid continuation:
a bad NAL, or a jump in the frame counter Tesla stores in every frame's SEI. Then it writes
a new file with just those frames and a freshly built moov.

Frame timing isn't stored in the frames, so the rebuilt clip uses a constant 36 fps
(Tesla records at ~36 fps with small jitter).
"""

import struct

from .telemetry import frame_telemetry, _unescape

TIMESCALE = 10000
FRAME_DURATION = 278          # 1/36 s at TIMESCALE
MIN_FRAMES = 36               # don't bother with less than a second
VALID_NAL_TYPES = {1, 5, 6, 7, 8, 9, 12}
SLICE_TYPES = {1, 5}
MP4_EPOCH_OFFSET = 2082844800  # seconds from 1904-01-01 to 1970-01-01


# ---------- reading ----------

def _find_mdat(f, file_size):
    """-> (data start, data end) of the mdat box, end clipped to the file."""
    pos = 0
    while pos + 8 <= file_size:
        f.seek(pos)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            size, = struct.unpack('>Q', f.read(8))
            header = 16
        elif size == 0:
            size = file_size - pos
        if kind == b'mdat':
            return pos + header, min(pos + size, file_size)
        if size < header or kind not in (b'ftyp', b'free', b'skip', b'wide', b'moov', b'uuid'):
            break
        pos += size
    raise ValueError('no mdat box')


def scan_frames(path):
    """-> dict with frames [(offset, size, keyframe)], sps, pps, first frame telemetry, mdat range."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()
        start, end = _find_mdat(f, file_size)
        frames = []
        sps = pps = None
        first_telemetry = None
        last_seq = None
        frame_start = None
        has_slice = keyframe = False
        pos = start
        f.seek(pos)
        buf = b''
        buf_pos = pos

        def read(at, n):
            nonlocal buf, buf_pos
            if not (buf_pos <= at and at + n <= buf_pos + len(buf)):
                f.seek(at)
                buf = f.read(max(n, 1 << 20))
                buf_pos = at
            return buf[at - buf_pos:at - buf_pos + n]

        while pos + 5 <= end:
            length, = struct.unpack('>I', read(pos, 4))
            if length == 0 or pos + 4 + length > end:
                break
            header = read(pos + 4, 1)[0]
            nal_type = header & 0x1F
            if header & 0x80 or nal_type not in VALID_NAL_TYPES:
                break
            # A slice whose first_mb_in_slice is 0 (ue(v) '1' bit) starts a new picture; clips
            # from older firmware have no SEI, so this is the only frame boundary they have.
            new_picture = nal_type in SLICE_TYPES and read(pos + 5, 1)[0] & 0x80
            if (nal_type in (6, 7, 8, 9) or new_picture) and has_slice:
                # A new frame starts: close the current one.
                frames.append((frame_start, pos - frame_start, keyframe))
                frame_start, has_slice, keyframe = pos, False, False
            if frame_start is None:
                frame_start = pos
            if nal_type == 7 and sps is None:
                sps = read(pos + 4, length)
            elif nal_type == 8 and pps is None:
                pps = read(pos + 4, length)
            elif nal_type == 6:
                values = frame_telemetry(read(pos, 4 + min(length, 4096)))
                if values:
                    seq = values.get('frame')
                    if last_seq is not None and seq is not None and seq != last_seq + 1:
                        # The frame counter jumped: these bytes belong to another clip.
                        frame_start = None
                        break
                    last_seq = seq
                    if first_telemetry is None:
                        first_telemetry = values
            elif nal_type in SLICE_TYPES:
                has_slice = True
                keyframe = keyframe or nal_type == 5
            pos += 4 + length
        if frame_start is not None and has_slice:
            frames.append((frame_start, pos - frame_start, keyframe))
        # Playback has to start on a keyframe.
        while frames and not frames[0][2]:
            frames.pop(0)
        return {'frames': frames, 'sps': sps, 'pps': pps, 'telemetry': first_telemetry,
                'mdat': (start, end), 'file_size': file_size}


class BitReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def bit(self):
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, n):
        value = 0
        for _ in range(n):
            value = value << 1 | self.bit()
        return value

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self):
        v = self.ue()
        return (v + 1) // 2 if v & 1 else -(v // 2)


def sps_dimensions(sps):
    """Width and height from an H.264 SPS NAL (including its 1-byte header)."""
    r = BitReader(_unescape(sps)[1:])
    profile = r.bits(8)
    r.bits(16)                       # constraint flags, level
    r.ue()                           # sps id
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.bit()
        r.ue(); r.ue(); r.bit()      # bit depths, qpprime flag
        if r.bit():                  # scaling matrices
            for i in range(8 if chroma != 3 else 12):
                if r.bit():
                    last = nxt = 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    r.ue()                           # log2_max_frame_num
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.bit(); r.se(); r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue(); r.bit()                  # max refs, gaps allowed
    width_mbs = r.ue() + 1
    height_units = r.ue() + 1
    frame_mbs_only = r.bit()
    if not frame_mbs_only:
        r.bit()
    r.bit()                          # direct_8x8
    crop = (0, 0, 0, 0)
    if r.bit():
        crop = (r.ue(), r.ue(), r.ue(), r.ue())
    sub_w = 2 if chroma in (1, 2) else 1
    sub_h = 2 if chroma == 1 else 1
    height_mul = 2 - frame_mbs_only
    width = width_mbs * 16 - sub_w * (crop[0] + crop[1])
    height = height_mul * height_units * 16 - sub_h * height_mul * (crop[2] + crop[3])
    return width, height


# ---------- writing ----------

def box(kind, *payload):
    data = b''.join(payload)
    return struct.pack('>I4s', 8 + len(data), kind) + data


def full_box(kind, version, flags, *payload):
    return box(kind, struct.pack('>I', version << 24 | flags), *payload)


MATRIX = struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def build_moov(frames, chunk_offset, sps, pps, width, height, created):
    count = len(frames)
    duration = count * FRAME_DURATION
    t = int(created) + MP4_EPOCH_OFFSET
    mvhd = full_box(b'mvhd', 0, 0, struct.pack('>IIII', t, t, TIMESCALE, duration),
                    struct.pack('>IH10x', 0x10000, 0x100), MATRIX, b'\0' * 24, struct.pack('>I', 2))
    tkhd = full_box(b'tkhd', 0, 3, struct.pack('>IIIII', t, t, 1, 0, duration), b'\0' * 8,
                    struct.pack('>hhH2x', 0, 0, 0), MATRIX, struct.pack('>II', width << 16, height << 16))
    mdhd = full_box(b'mdhd', 0, 0, struct.pack('>IIIIHH', t, t, TIMESCALE, duration, 0x55C4, 0))
    hdlr = full_box(b'hdlr', 0, 0, b'\0' * 4, b'vide', b'\0' * 12, b'VideoHandler\0')
    avcc = box(b'avcC', bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]), struct.pack('>H', len(sps)), sps,
               b'\x01', struct.pack('>H', len(pps)), pps)
    avc1 = box(b'avc1', b'\0' * 6, struct.pack('>H', 1), b'\0' * 16, struct.pack('>HH', width, height),
               struct.pack('>II', 0x480000, 0x480000), b'\0' * 4, struct.pack('>H', 1), b'\0' * 32,
               struct.pack('>Hh', 0x18, -1), avcc)
    stsd = full_box(b'stsd', 0, 0, struct.pack('>I', 1), avc1)
    stts = full_box(b'stts', 0, 0, struct.pack('>III', 1, count, FRAME_DURATION))
    sync = [i + 1 for i, f in enumerate(frames) if f[2]]
    stss = full_box(b'stss', 0, 0, struct.pack('>I%dI' % len(sync), len(sync), *sync))
    stsc = full_box(b'stsc', 0, 0, struct.pack('>IIII', 1, 1, count, 1))
    stsz = full_box(b'stsz', 0, 0, struct.pack('>II%dI' % count, 0, count, *(f[1] for f in frames)))
    if chunk_offset < 1 << 32:
        stco = full_box(b'stco', 0, 0, struct.pack('>II', 1, chunk_offset))
    else:
        stco = full_box(b'co64', 0, 0, struct.pack('>IQ', 1, chunk_offset))
    stbl = box(b'stbl', stsd, stts, stss, stsc, stsz, stco)
    vmhd = full_box(b'vmhd', 0, 1, b'\0' * 8)
    dinf = box(b'dinf', full_box(b'dref', 0, 0, struct.pack('>I', 1), full_box(b'url ', 0, 1)))
    minf = box(b'minf', vmhd, dinf, stbl)
    trak = box(b'trak', tkhd, box(b'mdia', mdhd, hdlr, minf))
    return box(b'moov', mvhd, trak)


def repair(src, dst, created_utc):
    """Rebuild src into dst. -> dict(frames, seconds, width, height, telemetry) or raises ValueError."""
    info = scan_frames(src)
    frames = info['frames']
    if len(frames) < MIN_FRAMES or not info['sps'] or not info['pps']:
        raise ValueError('only %d usable frames' % len(frames))
    width, height = sps_dimensions(info['sps'])
    ftyp = box(b'ftyp', b'mp42', struct.pack('>I', 0), b'mp42mp41isomiso2')
    first, last = frames[0][0], frames[-1][0] + frames[-1][1]
    mdat_size = last - first
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        fout.write(ftyp)
        large = mdat_size + 8 > 0xFFFFFFFF
        if large:
            fout.write(struct.pack('>I4sQ', 1, b'mdat', mdat_size + 16))
        else:
            fout.write(struct.pack('>I4s', mdat_size + 8, b'mdat'))
        data_start = fout.tell()
        fin.seek(first)
        remaining = mdat_size
        while remaining:
            chunk = fin.read(min(remaining, 8 << 20))
            if not chunk:
                raise ValueError('source ended early')
            fout.write(chunk)
            remaining -= len(chunk)
        # Frame offsets are relative to the first kept frame; one chunk holds them all.
        moov = build_moov(frames, data_start, info['sps'], info['pps'], width, height, created_utc)
        fout.write(moov)
    return {'frames': len(frames), 'seconds': len(frames) * FRAME_DURATION / TIMESCALE,
            'width': width, 'height': height, 'telemetry': info['telemetry']}
