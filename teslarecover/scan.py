"""Find every dashcam clip on the drive: live files, deleted directory entries and carved
clusters. Times come from the clip header, so nothing needs to be decrypted yet."""

import datetime
import re
import struct
import sys
import time

from .volume import Volume, to_runs

MP4_SIGNATURE = b'\0\0\0 ftypmp42'

# Tesla 2026.20+ encrypted clips are eCryptfs-style containers (format from
# https://github.com/XGxF3/tesla-dashcam-decrypt): a header that starts with the
# plaintext size and an eCryptfs marker, then 4096-byte encrypted pages from 0x2000.
ECRYPTFS_MARKER = 0x3c81b7f5
ENC_PAGE_SIZE = 0x1000
ENC_PAYLOAD_OFFSET = 0x2000
ENC_MAX_SIZE = 4 << 30
# Unix seconds (UTC), roughly when the clip ended.
ENC_TIMESTAMP_OFFSET = 0x1056
PLAIN_MAX_SIZE = 80 << 20
# Smaller encrypted files are event.json, thumb.png and readme files, not video.
MIN_CLIP_SIZE = 100_000
CLIP_SECONDS = 60

NAME_RE = re.compile(r'(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)-([a-z_]+)\.mp4', re.IGNORECASE)


def is_encrypted_clip(header):
    if len(header) < 0x18:
        return False
    plaintext_size, magic1, magic2 = struct.unpack('>QII', header[0x00:0x10])
    page_size, = struct.unpack('>I', header[0x14:0x18])
    return magic1 ^ magic2 == ECRYPTFS_MARKER and page_size == ENC_PAGE_SIZE and 0 < plaintext_size < ENC_MAX_SIZE


def is_clip_start(header):
    return header.startswith(MP4_SIGNATURE) or is_encrypted_clip(header)


def encrypted_size(plaintext_size):
    return ENC_PAYLOAD_OFFSET + -(-plaintext_size // ENC_PAGE_SIZE) * ENC_PAGE_SIZE


def parse_name(name):
    """'2026-10-11_16-15-43-front.mp4' -> (UTC epoch of the local time in the name, 'front')"""
    m = NAME_RE.fullmatch(name.rsplit('/', 1)[-1])
    if not m:
        return None, None
    local = datetime.datetime.strptime(m.group(1), '%Y-%m-%d_%H-%M-%S')
    return local.astimezone().timestamp(), m.group(2)


def read_header(vol, cluster):
    """Return (kind, plaintext_size, end_utc) for a clip starting at cluster, or None."""
    head = vol.read(cluster, 0x18)
    if head.startswith(MP4_SIGNATURE):
        return 'plain', None, None
    if not is_encrypted_clip(head):
        return None
    plaintext_size, = struct.unpack('>Q', head[:8])
    ts, = struct.unpack('>Q', vol.read(cluster, 8, ENC_TIMESTAMP_OFFSET))
    # Ignore implausible timestamps (corrupt metadata page).
    end = float(ts) if 1_500_000_000 < ts < 4_000_000_000 else None
    return 'encrypted', plaintext_size, end


def make_clip(source, start_cluster, clusters, size, kind, end_utc, name=None, path=None, gap=False, truncated=False):
    name_time, camera = parse_name(name) if name else (None, None)
    if name_time is not None:
        start = name_time
    elif end_utc is not None:
        start = end_utc - CLIP_SECONDS
    else:
        start = None
    folder = ''
    if path:
        parts = path.strip('/').split('/')
        # e.g. TeslaCam/EncryptedClips/SentryClips/<event>/<file> -> SentryClips/<event>
        folder = '/'.join(p for p in parts[1:-1] if p != 'EncryptedClips')
    return {
        'id': str(start_cluster),
        'source': source,                # live | deleted | carved
        'name': name,                    # Tesla's file name when a directory entry exists
        'camera': camera,
        'folder': folder,
        'path': path,
        'runs': [list(r) for r in to_runs(clusters)],
        'size': size,
        'encrypted': kind == 'encrypted',
        'start_utc': start,
        'end_utc': end_utc,
        'gap': gap,                      # carved clip had to skip clusters of live files
        'truncated': truncated,          # ran into the start of another clip: data is incomplete
    }


def name_matches_header(name_time, end_utc):
    """The name is local time, the header UTC: they agree if they differ by whole hours (+ ~1 min)."""
    if name_time is None or end_utc is None:
        return True
    diff = end_utc - name_time
    return any(abs(diff - h * 1800) <= 120 for h in range(-28, 29))


class Scanner:
    def __init__(self, drive, progress=None, cancelled=None):
        self.drive = drive
        self.progress = progress or (lambda phase, done, total: None)
        self.is_cancelled = cancelled or (lambda: False)

    def run(self):
        vol = Volume(self.drive)
        try:
            return self._run(vol)
        finally:
            vol.close()

    def _run(self, vol):
        clips = []
        self.progress('Reading folders', 0, 1)
        walked = list(vol.walk())
        # Clusters owned by live files and folders. Everything else may hold deleted clips - including
        # clusters the bitmap marks as allocated but no file owns (the car leaves gigabytes of those).
        claimed = set(vol.cluster_chain(vol.root_cluster)) | set(range(2, 5))
        for e in walked:
            if not e.deleted:
                claimed.update(e.clusters)
        entries = [e for e in walked if not e.is_dir and e.name.lower().endswith('.mp4') and e.size > 0]
        self.progress('Reading folders', 1, 1)

        live = [e for e in entries if not e.deleted]
        for i, e in enumerate(live):
            header = read_header(vol, e.first_cluster) if e.first_cluster >= 2 else None
            kind, _, end = header if header else ('unknown', None, None)
            clips.append(make_clip('live', e.first_cluster, e.clusters, e.size, kind, end, e.name, e.path))
            claimed.update(e.clusters)
            if i % 50 == 0:
                if self.is_cancelled():
                    raise InterruptedError('scan cancelled')
                self.progress('Reading existing clips', i, len(live))

        # Deleted directory entries still hold the name, first cluster and size.
        seen = {c['id'] for c in clips}
        for e in (e for e in entries if e.deleted):
            if str(e.first_cluster) in seen or e.first_cluster in claimed:
                continue
            header = read_header(vol, e.first_cluster)
            if not header:
                continue
            kind, plaintext_size, end = header
            if kind == 'encrypted' and encrypted_size(plaintext_size) != e.size:
                continue
            if not name_matches_header(parse_name(e.name)[0], end):
                continue
            clusters, gap, truncated = self._deleted_clusters(vol, e, claimed)
            if not clusters:
                continue
            seen.add(str(e.first_cluster))
            claimed.update(clusters)
            clips.append(make_clip('deleted', e.first_cluster, clusters, e.size, kind, end, e.name, e.path, gap, truncated))

        clips += self._carve(vol, claimed)
        clips.sort(key=lambda c: (c['start_utc'] or 0, int(c['id'])))
        return {
            'drive': self.drive,
            'scanned_at': time.time(),
            'filesystem': vol.fs_name,
            'cluster_size': vol.cluster_size,
            'clips': clips,
        }

    def _deleted_clusters(self, vol, e, claimed):
        count = -(-e.size // vol.cluster_size)
        if not e.no_fat_chain:
            # A fragmented file's FAT chain sometimes survives deletion; use it if it is complete and free.
            chain = vol.cluster_chain(e.first_cluster)
            if len(chain) == count and not any(c in claimed for c in chain):
                return chain, False, False
        return self._free_run(vol, e.first_cluster, count, claimed)

    def _free_run(self, vol, start, count, claimed):
        """The next count unclaimed clusters from start (skipping live data in between), stopping
        early where another clip starts."""
        clusters = []
        c = start
        gap = False
        last = vol.cluster_count + 2
        while len(clusters) < count and c < last:
            if c in claimed:
                gap = gap or bool(clusters)
            else:
                clusters.append(c)
            c += 1
        # If another clip starts inside the run, the rest of this one was elsewhere (fragmented) or overwritten.
        for i in range(1, len(clusters), 64):
            batch = clusters[i:i + 64]
            for j, head in enumerate(vol.read_heads(batch)):
                if is_clip_start(head):
                    return clusters[:i + j], gap, True
        return clusters, gap, False

    def _carve(self, vol, claimed):
        clips = []
        last = vol.cluster_count + 2
        plain = None  # (start, clusters) of an unencrypted clip whose end isn't known yet

        def close_plain():
            if plain:
                start, clusters = plain
                clips.append(make_clip('carved', start, clusters, len(clusters) * vol.cluster_size, 'plain', None))

        cluster = 2
        reported = 0
        while cluster < last:
            if self.is_cancelled():
                raise InterruptedError('scan cancelled')
            if time.time() - reported > 0.5:
                reported = time.time()
                self.progress('Searching free space for deleted clips', cluster, last)
            if cluster in claimed:
                cluster += 1
                continue
            header = read_header(vol, cluster)
            if header is None:
                if plain:
                    plain[1].append(cluster)
                    if len(plain[1]) * vol.cluster_size >= PLAIN_MAX_SIZE:
                        close_plain()
                        plain = None
                cluster += 1
                continue
            close_plain()
            plain = None
            kind, plaintext_size, end = header
            if kind == 'plain':
                plain = (cluster, [cluster])
                cluster += 1
                continue
            size = encrypted_size(plaintext_size)
            if plaintext_size < MIN_CLIP_SIZE:
                cluster += -(-size // vol.cluster_size)
                continue
            clusters, gap, truncated = self._free_run(vol, cluster, -(-size // vol.cluster_size), claimed)
            claimed.update(clusters)
            clips.append(make_clip('carved', cluster, clusters, size, kind, end, gap=gap, truncated=truncated))
            cluster = clusters[-1] + 1
        close_plain()
        self.progress('Searching free space for deleted clips', last, last)
        return clips


def main():
    import collections
    import json
    if len(sys.argv) < 2:
        sys.exit('Usage: python -m teslarecover.scan D: [out.json]')
    t = time.time()
    last = [0]

    def progress(phase, done, total):
        if time.time() - last[0] > 2:
            last[0] = time.time()
            print('%s: %d/%d' % (phase, done, total), flush=True)

    result = Scanner(sys.argv[1], progress).run()
    clips = result['clips']
    print('%d clips in %.0f s' % (len(clips), time.time() - t))
    print(collections.Counter((c['source'], c['encrypted'], c['camera'] is not None) for c in clips))
    if len(sys.argv) > 2:
        with open(sys.argv[2], 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=1)


if __name__ == '__main__':
    main()
