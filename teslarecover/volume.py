"""Read-only access to a FAT32/exFAT volume: boot sector, FAT, allocation bitmap and
directory entries, including deleted ones. Based on run.py."""

import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

END_OF_CHAIN = 0x0FFFFFF7


class AlignedReader:
    """Raw Windows volumes only allow sector-aligned seeks/reads, so round each read out to whole sectors."""

    def __init__(self, path, align=4096):
        self.f = open(path, 'rb', buffering=0)
        self.align = align

    def read_at(self, pos, n):
        start = pos - pos % self.align
        end = pos + n
        end += -end % self.align
        self.f.seek(start)
        data = self.f.read(end - start)
        return data[pos - start:pos - start + n]

    def close(self):
        self.f.close()


@dataclass
class Entry:
    path: str              # '/TeslaCam/EncryptedClips/RecentClips/2026-...-front.mp4'
    name: str
    is_dir: bool
    deleted: bool
    first_cluster: int
    size: int
    no_fat_chain: bool = False
    clusters: list = field(default_factory=list)


def raw_path(drive):
    """'D:' or 'D:\\' -> '\\\\.\\D:' on Windows; anything else (an image file) is used as is."""
    if os.name == 'nt' and len(drive.rstrip('\\/')) == 2 and drive[1] == ':':
        return '\\\\.\\' + drive[:2]
    return drive


class Volume:
    def __init__(self, path):
        path = raw_path(path)
        self.path = path
        self._local = threading.local()
        self._pool = None
        self.reader = AlignedReader(path) if path.startswith('\\\\.\\') else AlignedReader(path, 1)
        boot = self.reader.read_at(0, 512)
        self.is_exfat = boot[3:8] == b'EXFAT'
        if self.is_exfat:
            self.bytes_per_sector = 1 << boot[0x6C]
            self.sectors_per_cluster = 1 << boot[0x6D]
            self.fat_offset, self.fat_length, self.heap_offset, self.cluster_count, self.root_cluster = \
                struct.unpack('<IIIII', boot[0x50:0x64])
        elif boot[0x52:0x57] == b'FAT32':
            self.bytes_per_sector, self.sectors_per_cluster, self.fat_offset, fats, _, total16 = \
                struct.unpack('<HBHBHH', boot[0x0B:0x15])
            total, = struct.unpack('<I', boot[0x20:0x24])
            self.fat_length, = struct.unpack('<I', boot[0x24:0x28])
            self.root_cluster, = struct.unpack('<I', boot[0x2C:0x30])
            self.heap_offset = self.fat_offset + fats * self.fat_length
            self.cluster_count = ((total16 or total) - self.heap_offset) // self.sectors_per_cluster
        else:
            raise ValueError('not an exFAT or FAT32 volume')
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster

        # Only the first FAT is needed; any others are mirrors (or a TexFAT copy).
        fat = self.reader.read_at(self.fat_offset * self.bytes_per_sector, self.fat_length * self.bytes_per_sector)
        self.fat = [e & 0x0FFFFFFF for e in struct.unpack('<%dI' % (len(fat) // 4), fat)]
        self.bitmap = self._read_bitmap()

    def close(self):
        if self._pool:
            self._pool.shutdown()
        self.reader.close()

    def read_heads(self, clusters, n=0x18):
        """Read the first n bytes of many clusters, in parallel (USB drives queue small reads well)."""
        if self._pool is None:
            self._pool = ThreadPoolExecutor(4)

        def read_one(cluster):
            reader = getattr(self._local, 'reader', None)
            if reader is None:
                reader = self._local.reader = AlignedReader(self.path, self.reader.align)
            return reader.read_at(self.cluster_offset(cluster), n)
        return list(self._pool.map(read_one, clusters))

    @property
    def fs_name(self):
        return 'exFAT' if self.is_exfat else 'FAT32'

    def cluster_offset(self, cluster):
        return (self.heap_offset + (cluster - 2) * self.sectors_per_cluster) * self.bytes_per_sector

    def read(self, cluster, n, offset=0):
        return self.reader.read_at(self.cluster_offset(cluster) + offset, n)

    def read_cluster(self, cluster):
        return self.read(cluster, self.cluster_size)

    def is_allocated(self, cluster):
        i = cluster - 2
        return bool(self.bitmap[i >> 3] & (1 << (i & 7))) if 0 <= i < self.cluster_count else True

    def free_cluster_count(self):
        used = sum(bin(b).count('1') for b in self.bitmap)
        return self.cluster_count - used

    def cluster_chain(self, first, length=None, no_fat_chain=False):
        if first < 2:
            return []
        if no_fat_chain:
            # exFAT files flagged NoFatChain are contiguous and have no FAT entries.
            return list(range(first, first + -(-length // self.cluster_size)))
        clusters = []
        cluster = first
        while 2 <= cluster < END_OF_CHAIN and cluster < len(self.fat) and len(clusters) <= self.cluster_count:
            clusters.append(cluster)
            cluster = self.fat[cluster]
        return clusters

    def read_clusters(self, clusters, length):
        """Yield the file's bytes cluster run by cluster run, up to length bytes."""
        remaining = length
        for start, count in to_runs(clusters):
            if remaining <= 0:
                break
            n = min(count * self.cluster_size, remaining)
            # Read big runs in 8 MiB pieces.
            pos = 0
            while pos < n:
                chunk = self.reader.read_at(self.cluster_offset(start) + pos, min(8 << 20, n - pos))
                if not chunk:
                    return
                yield chunk
                pos += len(chunk)
            remaining -= n

    def _read_bitmap(self):
        if not self.is_exfat:
            # FAT32 has no bitmap: a cluster is free when its FAT entry is 0.
            bits = bytearray(-(-self.cluster_count // 8))
            for i in range(self.cluster_count):
                if self.fat[i + 2] != 0:
                    bits[i >> 3] |= 1 << (i & 7)
            return bytes(bits)
        root = b''.join(self.read_cluster(c) for c in self.cluster_chain(self.root_cluster))
        for i in range(0, len(root), 32):
            if root[i] == 0x81:
                first, length = struct.unpack('<IQ', root[i + 0x14:i + 0x20])
                return b''.join(self.read_clusters(self.cluster_chain(first, length, True), length))
            if root[i] == 0x00:
                break
        raise ValueError('exFAT allocation bitmap not found')

    def walk(self, include_deleted=True):
        """Yield every Entry below the root, depth first. Deleted entries are yielded with
        deleted=True; deleted directories are entered too when their clusters look intact."""
        if self.is_exfat:
            yield from self._walk_exfat(self.cluster_chain(self.root_cluster), '', include_deleted, False, set())
        else:
            yield from self._walk_fat32(self.root_cluster, '', set())

    def _walk_exfat(self, clusters, path, include_deleted, in_deleted, seen):
        data = b''.join(self.read_cluster(c) for c in clusters)
        i = 0
        while i + 32 <= len(data):
            entry_type = data[i]
            if entry_type == 0x00:
                break
            if entry_type not in (0x85, 0x05):
                i += 32
                continue
            deleted = entry_type == 0x05 or in_deleted
            secondary = data[i + 1]
            attributes, = struct.unpack('<H', data[i + 4:i + 6])
            entry_set = data[i + 32:i + 32 + secondary * 32]
            if not 2 <= secondary <= 18 or len(entry_set) < 64 or entry_set[0] & 0x7F != 0x40:
                i += 32
                continue
            i += 32 + secondary * 32
            if deleted and not include_deleted:
                continue

            flags, _, name_length = entry_set[1], entry_set[2], entry_set[3]
            first, size = struct.unpack('<IQ', entry_set[0x14:0x20])
            name = b''.join(entry_set[j + 2:j + 32] for j in range(32, len(entry_set), 32)
                            if entry_set[j] & 0x7F == 0x41)
            name = name.decode('utf-16-le', 'replace')[:name_length]
            no_fat_chain = bool(flags & 0x02)
            if deleted:
                # The FAT chain of a freed file can't be trusted; recovery assumes contiguous clusters.
                clusters_ = self.cluster_chain(first, size, True) if size else []
            else:
                clusters_ = self.cluster_chain(first, size, no_fat_chain)
            entry = Entry(path + '/' + name, name, bool(attributes & 0x10), deleted, first, size,
                          no_fat_chain, clusters_)
            yield entry
            if entry.is_dir and clusters_ and first not in seen and len(clusters_) < 64 \
                    and all(c - 2 < self.cluster_count for c in clusters_):
                seen.add(first)
                yield from self._walk_exfat(clusters_, entry.path, include_deleted, deleted, seen)

    def _walk_fat32(self, cluster, path, seen):
        data = b''.join(self.read_cluster(c) for c in self.cluster_chain(cluster))
        lfn = b''
        for i in range(0, len(data), 32):
            e = data[i:i + 32]
            if e[0] == 0x00:
                break
            if e[11] == 0x0F:
                lfn = e[1:11] + e[14:26] + e[28:32] + lfn
                continue
            if e[0] == 0xE5 or e[11] & 0x08:
                lfn = b''
                continue
            short = e[0:8].decode('cp437').rstrip()
            ext = e[8:11].decode('cp437').rstrip()
            name = lfn.decode('utf-16-le', 'replace').split('\0')[0].replace('\uffff', '') if lfn else \
                (short + '.' + ext if ext else short)
            lfn = b''
            if short in ('.', '..'):
                continue
            hi, = struct.unpack('<H', e[0x14:0x16])
            lo, size = struct.unpack('<HI', e[0x1A:0x20])
            first = hi << 16 | lo
            is_dir = bool(e[11] & 0x10)
            entry = Entry(path + '/' + name, name, is_dir, False, first, size, False, self.cluster_chain(first))
            yield entry
            if is_dir and first not in seen:
                seen.add(first)
                yield from self._walk_fat32(first, entry.path, seen)


def to_runs(clusters):
    """[5,6,7,10,11] -> [(5,3),(10,2)]"""
    runs = []
    for c in clusters:
        if runs and runs[-1][0] + runs[-1][1] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])
    return [tuple(r) for r in runs]


def from_runs(runs):
    return [c for start, count in runs for c in range(start, start + count)]
