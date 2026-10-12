"""Per-frame telemetry from the SEI NAL units Tesla puts in every video frame.

Each frame carries an SEI (NAL type 6) with a user_data_unregistered payload (type 5)
that starts with 'BBBi' followed by a protobuf. The data is the same in all six
cameras; it has no timestamp, so frame times come from the track's stts table.
"""

import struct

from .naming import boxes, findBox

# protobuf field number -> name
FIELDS = {
    1: 'version', 2: 'gear', 3: 'frame', 4: 'speed', 5: 'accelerator', 6: 'steering',
    7: 'blinker_left', 8: 'blinker_right', 9: 'brake', 10: 'autopilot',
    11: 'lat', 12: 'lon', 13: 'heading', 14: 'accel_x', 15: 'accel_y', 16: 'accel_z',
}


def _table(f, box, entry_format):
    """Read a full-box table: version/flags, count, then entries."""
    f.seek(box[0] + 4)
    count, = struct.unpack('>I', f.read(4))
    size = struct.calcsize(entry_format)
    data = f.read(count * size)
    return [struct.unpack_from(entry_format, data, i * size) for i in range(count)]


def _video_stbl(f, end):
    moov = findBox(f, 0, end, [b'moov'])
    if moov is None:
        raise ValueError('no moov box')
    for box_type, start, stop in boxes(f, *moov):
        if box_type != b'trak':
            continue
        hdlr = findBox(f, start, stop, [b'mdia', b'hdlr'])
        if hdlr:
            f.seek(hdlr[0] + 8)
            if f.read(4) != b'vide':
                continue
        mdhd = findBox(f, start, stop, [b'mdia', b'mdhd'])
        f.seek(mdhd[0])
        version = f.read(1)[0]
        f.seek(mdhd[0] + (20 if version == 1 else 12))
        timescale, = struct.unpack('>I', f.read(4))
        return findBox(f, start, stop, [b'mdia', b'minf', b'stbl']), timescale
    raise ValueError('no video track')


def _samples(f, stbl):
    """-> list of (offset, size, time in timescale units) for every video sample."""
    def child(name):
        return findBox(f, stbl[0], stbl[1], [name])

    stsz = child(b'stsz')
    f.seek(stsz[0] + 4)
    fixed, count = struct.unpack('>II', f.read(8))
    sizes = [fixed] * count if fixed else list(struct.unpack('>%dI' % count, f.read(4 * count)))

    if child(b'co64'):
        chunks = [c[0] for c in _table(f, child(b'co64'), '>Q')]
    else:
        chunks = [c[0] for c in _table(f, child(b'stco'), '>I')]
    stsc = _table(f, child(b'stsc'), '>III')
    stts = _table(f, child(b'stts'), '>II')

    offsets = []
    for i, (first, per_chunk, _) in enumerate(stsc):
        last = stsc[i + 1][0] - 1 if i + 1 < len(stsc) else len(chunks)
        for chunk in range(first, last + 1):
            pos = chunks[chunk - 1]
            for _ in range(per_chunk):
                if len(offsets) == count:
                    break
                offsets.append(pos)
                pos += sizes[len(offsets) - 1]

    times = []
    t = 0
    for n, delta in stts:
        for _ in range(n):
            times.append(t)
            t += delta
    return [(o, s, times[i] if i < len(times) else t) for i, (o, s) in enumerate(zip(offsets, sizes))]


def _unescape(data):
    """Remove H.264 emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _varint(data, pos):
    value = shift = 0
    while True:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def decode_protobuf(data):
    values = {}
    pos = 0
    try:
        while pos < len(data):
            key, pos = _varint(data, pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = _varint(data, pos)
            elif wire == 1:
                value, = struct.unpack_from('<d', data, pos)
                pos += 8
            elif wire == 5:
                value, = struct.unpack_from('<f', data, pos)
                pos += 4
            elif wire == 2:
                n, pos = _varint(data, pos)
                pos += n
                continue
            else:
                break
            if field in FIELDS:
                values[FIELDS[field]] = value
    except (IndexError, struct.error):
        pass
    return values


def _sei_payloads(nal):
    """Yield (payload type, payload) for the messages in an unescaped SEI NAL."""
    pos = 1
    while pos < len(nal) and nal[pos] != 0x80:
        ptype = psize = 0
        while nal[pos] == 0xFF:
            ptype += 255
            pos += 1
        ptype += nal[pos]
        pos += 1
        while nal[pos] == 0xFF:
            psize += 255
            pos += 1
        psize += nal[pos]
        pos += 1
        yield ptype, nal[pos:pos + psize]
        pos += psize


def frame_telemetry(sample):
    pos = 0
    while pos + 4 <= len(sample):
        n, = struct.unpack_from('>I', sample, pos)
        nal = sample[pos + 4:pos + 4 + n]
        pos += 4 + n
        if not nal or nal[0] & 0x1F != 6:
            continue
        try:
            for ptype, payload in _sei_payloads(_unescape(nal)):
                if ptype == 5 and b'BBBi' in payload[:8]:
                    return decode_protobuf(payload[payload.index(b'BBBi') + 4:])
        except IndexError:
            continue
    return None


def read_telemetry(path):
    """-> {'fps': float, 'frames': n, 't': [seconds...], <field>: [value per frame]} (columnar)"""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        end = f.tell()
        stbl, timescale = _video_stbl(f, end)
        samples = _samples(f, stbl)
        result = {'t': [], **{name: [] for name in FIELDS.values()}}
        for offset, size, t in samples:
            f.seek(offset)
            # The SEI comes before the picture data; the first few KB are enough.
            values = frame_telemetry(f.read(min(size, 4096)))
            result['t'].append(round(t / timescale, 4))
            for name in FIELDS.values():
                # proto3 leaves out fields that are 0 / false.
                result[name].append(None if values is None else values.get(name, 0))
        duration = samples[-1][2] / timescale if samples else 0
        result['frames'] = len(samples)
        result['fps'] = round((len(samples) - 1) / duration, 3) if duration else 0
        return result
