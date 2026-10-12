"""MP4 metadata, camera assignment and final file names for decrypted Tesla clips.

The recording start comes from moov/mvhd creation_time, which Tesla stores in UTC;
names use this computer's local time zone (the same local time Tesla uses in its own
file names). Tesla puts the area in the title tag and the street in the comment tag.

Camera: the front camera is the only one recorded at 2896x1876. The car writes each
minute's six files back to back in CAMERA_ORDER, so a clip that starts on the cluster
after another one ends is the next camera.
"""

import collections
import datetime
import re
import struct

MP4_EPOCH = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FRONT_WIDTH = 2896
SIDE_CAMERAS = ('left_repeater', 'right_repeater', 'back', 'left_pillar', 'right_pillar')
CAMERA_ORDER = ('front',) + SIDE_CAMERAS
ENC_HEADER_SIZE = 0x2000
ENC_PAGE_SIZE = 0x1000


def boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, boxType = struct.unpack('>I4s', f.read(8))
        headerSize = 8
        if size == 1:
            size, = struct.unpack('>Q', f.read(8))
            headerSize = 16
        elif size == 0:
            size = end - pos
        if size < headerSize or pos + size > end:
            raise ValueError('corrupt box at offset %d' % pos)
        yield boxType, pos + headerSize, pos + size
        pos += size


def findBox(f, start, end, path):
    for boxType, dataStart, dataEnd in boxes(f, start, end):
        if boxType == path[0]:
            if len(path) == 1:
                return dataStart, dataEnd
            # 'meta' has a 4-byte version/flags field before its children.
            return findBox(f, dataStart + (4 if boxType == b'meta' else 0), dataEnd, path[1:])
    return None


def readMetadata(path):
    """-> (recording start as UTC datetime, video width, title, comment)"""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        end = f.tell()

        mvhd = findBox(f, 0, end, [b'moov', b'mvhd'])
        if mvhd is None:
            raise ValueError('no moov/mvhd box')
        f.seek(mvhd[0])
        data = f.read(12)
        if data[0] == 1:
            creationTime, = struct.unpack('>Q', data[4:12])
        else:
            creationTime, = struct.unpack('>I', data[4:8])

        # tkhd ends with the track width and height as 16.16 fixed point.
        tkhd = findBox(f, 0, end, [b'moov', b'trak', b'tkhd'])
        width = 0
        if tkhd:
            f.seek(tkhd[1] - 8)
            width = struct.unpack('>I', f.read(4))[0] >> 16

        tags = dict()
        ilst = findBox(f, 0, end, [b'moov', b'trak', b'udta', b'meta', b'ilst'])
        if ilst:
            for tag, dataStart, dataEnd in boxes(f, *ilst):
                value = findBox(f, dataStart, dataEnd, [b'data'])
                if value:
                    # data box: 4-byte type, 4-byte locale, then the UTF-8 value.
                    f.seek(value[0] + 8)
                    tags[tag] = f.read(value[1] - value[0] - 8).decode('utf-8', 'replace').strip()

    recorded = MP4_EPOCH + datetime.timedelta(seconds=creationTime)
    return recorded, width, tags.get(b'\xa9nam', ''), tags.get(b'\xa9cmt', '')


def readListing(path):
    """Map first cluster -> camera name for clips listed by `run.py ... list`."""
    with open(path, 'rb') as f:
        raw = f.read()
    # PowerShell's > writes UTF-16; cmd and bash write UTF-8.
    text = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8', 'replace')
    cameras = dict()
    name = None
    for line in text.splitlines():
        match = re.search(r'Filename:\s+(.*\S)', line)
        if match:
            name = match.group(1)
            continue
        match = re.search(r'Clusters:\s+(\d+)\s+-', line)
        if match and name:
            camera = re.fullmatch(r'\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d-(\w+)\.mp4', name)
            if camera:
                cameras[int(match.group(1))] = camera.group(1)
            name = None
    return cameras


def encryptedClusters(plaintextSize, clusterSize):
    # Encrypted clip on the drive: two 4096-byte header pages, then whole 4096-byte pages.
    length = ENC_HEADER_SIZE + -(-plaintextSize // ENC_PAGE_SIZE) * ENC_PAGE_SIZE
    return -(-length // clusterSize)


def assignCameras(clips, listed, clusterSize=131072):
    """clips: list of dicts with 'cluster', 'size', 'recorded', 'width' and optionally 'end_cluster'
    (the cluster after the clip's last one). Sets 'camera'."""
    groups = collections.defaultdict(list)
    for clip in clips:
        groups[clip['recorded']].append(clip)

    def endOf(clip):
        return clip.get('end_cluster') or clip['cluster'] + encryptedClusters(clip['size'], clusterSize)

    for group in groups.values():
        for clip in group:
            clip['camera'] = listed.get(clip['cluster'])
            if clip['camera'] is None and clip['width'] == FRONT_WIDTH:
                clip['camera'] = 'front'

        # The car writes a minute's files back to back in CAMERA_ORDER, so a clip that
        # starts right where a known camera's clip ends is the next camera (and vice versa).
        byStart = {clip['cluster']: clip for clip in group}
        byEnd = {endOf(clip): clip for clip in group}
        changed = True
        while changed:
            changed = False
            for clip in group:
                if clip['camera'] not in CAMERA_ORDER:
                    continue
                index = CAMERA_ORDER.index(clip['camera'])
                neighbours = ((byStart.get(endOf(clip)), index + 1), (byEnd.get(clip['cluster']), index - 1))
                for neighbour, neighbourIndex in neighbours:
                    if neighbour and neighbour['camera'] is None and 0 <= neighbourIndex < len(CAMERA_ORDER):
                        neighbour['camera'] = CAMERA_ORDER[neighbourIndex]
                        changed = True

        # A fragmented clip breaks the chain, but if all five side cameras are here their
        # order on disk still tells them apart.
        side = sorted([c for c in group if c['camera'] != 'front'], key=lambda c: c['cluster'])
        if len(side) == len(SIDE_CAMERAS):
            for clip, camera in zip(side, SIDE_CAMERAS):
                if clip['camera'] is None:
                    clip['camera'] = camera

        for clip in group:
            if clip['camera'] is None:
                clip['camera'] = 'side'


def localTime(utc, tzOffset=None):
    if tzOffset is None:
        return utc.astimezone()
    return utc + datetime.timedelta(hours=tzOffset)


def clipName(recorded, camera, extra=(), tzOffset=None, suffix=''):
    """'2026-10-11_16-15-43-front_Downtown_Main-Street.mp4' (local time)"""
    parts = ['%s-%s%s' % (localTime(recorded, tzOffset).strftime('%Y-%m-%d_%H-%M-%S'), camera, suffix)]
    parts += [str(p) for p in extra if p]
    return INVALID_CHARS.sub('_', '_'.join(parts)) + '.mp4'
