#!/usr/bin/env python3

import os
import sys
import struct


def dumpBinary(data):
    offset = 0
    for byte in data:
        print('%02X ' % byte, end='')
        offset += 1
        if offset % 16 == 0:
            print('')
        elif offset % 8 == 0:
            print(' ', end='')
    return


if len(sys.argv) < 3:
    print('Usage: '+sys.argv[0]+' input outputdir [carve | list]')
    sys.exit()

mode = sys.argv[3] if len(sys.argv) > 3 else 'carve'

class AlignedReader:
    """Raw Windows volumes only allow sector-aligned seeks/reads, so round each read out to whole sectors."""

    def __init__(self, path, align=4096):
        self.f = open(path, 'rb', buffering=0)
        self.align = align
        self.pos = 0

    def seek(self, pos):
        self.pos = pos

    def tell(self):
        return self.pos

    def read(self, n):
        start = self.pos - self.pos % self.align
        end = self.pos + n
        end += -end % self.align
        self.f.seek(start)
        data = self.f.read(end - start)
        out = data[self.pos - start:self.pos - start + n]
        self.pos += len(out)
        return out

    def close(self):
        self.f.close()


inputPath = sys.argv[1]
if os.name == 'nt' and len(inputPath.rstrip('\\/')) == 2 and inputPath[1] == ':':
    # "D:" alone means "current directory on D:" - open the raw volume instead.
    inputPath = '\\\\.\\' + inputPath[:2]

if inputPath.startswith('\\\\.\\'):
    f = AlignedReader(inputPath)
else:
    f = open(inputPath, 'rb')

bootSector = f.read(512)

isExFat = bootSector[3:8] == b'EXFAT'

if isExFat:
    bytesPerSectorShift,    = struct.unpack("<B", bootSector[0x6C:0x6D])
    bytesPerSector          = 1 << bytesPerSectorShift
    sectorsPerClusterShift, = struct.unpack("<B", bootSector[0x6D:0x6E])
    sectorsPerCluster       = 1 << sectorsPerClusterShift
    reservedSectors,        = struct.unpack("<I", bootSector[0x50:0x54])
    numberOfFATs,           = struct.unpack("<B", bootSector[0x6E:0x6F])
    maxRootDirEntries       = 0
    totalSectors,           = struct.unpack("<Q", bootSector[0x48:0x50])
    sectorsPerFAT,          = struct.unpack("<I", bootSector[0x54:0x58])
    rootDirectoryCluster,   = struct.unpack("<I", bootSector[0x60:0x64])
    clusterHeapOffset,      = struct.unpack("<I", bootSector[0x58:0x5C])
    clusterCount,           = struct.unpack("<I", bootSector[0x5C:0x60])
else:
    bytesPerSector,       = struct.unpack("<H", bootSector[0x0B:0x0D])
    sectorsPerCluster,    = struct.unpack("<B", bootSector[0x0D:0x0E])
    reservedSectors,      = struct.unpack("<H", bootSector[0x0E:0x10])
    numberOfFATs,         = struct.unpack("<B", bootSector[0x10:0x11])
    maxRootDirEntries,    = struct.unpack("<H", bootSector[0x11:0x13])
    totalSectors,         = struct.unpack("<H", bootSector[0x13:0x15])
    sectorsPerFAT,        = struct.unpack("<I", bootSector[0x24:0x28])
    rootDirectoryCluster, = struct.unpack("<I", bootSector[0x2c:0x30])

if totalSectors == 0:
    totalSectors, = struct.unpack("<I", bootSector[0x20:0x24])

if isExFat:
    totalClusters = clusterCount
else:
    totalClusters = totalSectors//sectorsPerCluster
    clusterHeapOffset = reservedSectors+numberOfFATs*sectorsPerFAT+maxRootDirEntries*32//bytesPerSector
bytesPerCluster = bytesPerSector*sectorsPerCluster

print('Bytes per sector: %d' % bytesPerSector)
print('Sectors per cluster: %d' % sectorsPerCluster)
print('Reserved sectors: %d' % reservedSectors)
print('Number of FATs: %d' % numberOfFATs)
print('Maximum root directory entries: %d' % maxRootDirEntries)
print('Total sectors: %d' % totalSectors)
print('Total clusters: %d' % totalClusters)
print('Sectors per FAT: %d' % sectorsPerFAT)
print('Root Directory Cluster: %d' % rootDirectoryCluster)


def sect2byte(sect):
    return sect*bytesPerSector


def clust2byte(clust):
    return sect2byte(clusterHeapOffset+(clust-2)*sectorsPerCluster)


def isNonzero(data):
    for x in data:
        if x != 0x00:
            return True
    return False


fatEntries = list()

print('Reading FATs at offset %d . . .' % sect2byte(reservedSectors))

f.seek(sect2byte(reservedSectors))

# Only the first FAT is needed; any others are mirrors (or a TexFAT copy).
fat = f.read(sectorsPerFAT*bytesPerSector)
for entry in struct.unpack("<%dI" % (len(fat)//4), fat):
    fatEntries.append(entry & 0x0FFFFFFF)


def readDirectory(directoryCluster, tabs, path='/', root=False):
    pad = list()
    for i in range(0, tabs):
        pad.append('\t')
    pad = ''.join(pad)

    while True:
        f.seek(clust2byte(directoryCluster))
        lfn = b''
        for i in range(bytesPerCluster // 32):
            entry = f.read(32)

            shortFilename = entry[0:8]
            extension = entry[8:11]
            attributes = entry[11]
            lastModifiedTime, = struct.unpack("<H", entry[0x16:0x18])
            lastModifiedDate, = struct.unpack("<H", entry[0x18:0x1a])
            clusterLow,       = struct.unpack("<H", entry[0x1a:0x1c])
            clusterHigh,      = struct.unpack("<H", entry[0x14:0x16])
            filesize,         = struct.unpack("<I", entry[28:32])
            cluster = (clusterHigh << 16) | clusterLow

            if lfn != b'':
                # LFN is UTF-16LE, terminated by 0x0000 and padded with 0xFFFF.
                filename = lfn.decode('utf-16-le', 'replace').split('\0')[0].replace('￿', '')
            else:
                name = shortFilename.decode('cp437').rstrip()
                ext = extension.decode('cp437').rstrip()
                filename = '%s.%s' % (name, ext) if ext else name

            if attributes == 0 and cluster == 0:
                continue

            if attributes != 0x0F:
                # Not a long file name.
                print('\n%sFilename:   %s' % (pad, filename))
                print('%sAttributes: 0x%02X' % (pad, attributes))
                print('%sFilesize:   %d' % (pad, filesize))
                print('%sLast Modified: %d-%d-%d %d:%02d:%02d' % (pad,
                                                                  1980 +
                                                                  ((lastModifiedDate & 0xfe00) >> 9), (
                                                                      lastModifiedDate & 0x1e0) >> 5, lastModifiedDate & 0x1f,
                                                                  (lastModifiedTime & 0xf800) >> 11, (
                                                                      lastModifiedTime & 0x7e0) >> 5, lastModifiedTime & 0x1f
                                                                  ))
                lfn = b''

                firstCluster = cluster
                curCluster = cluster
                lastCluster = firstCluster - 1
                while curCluster < 0x0FFFFFF0:
                    if curCluster != lastCluster + 1:
                        print('%sClusters: %d - %d' % (pad,
                                                       firstCluster, lastCluster))
                        firstCluster = curCluster
                    lastCluster = curCluster
                    curCluster = fatEntries[curCluster]
                print('%sClusters: %d - %d' % (pad, firstCluster, lastCluster))

            if attributes == 0x0F:
                # Long file name.
                lfn = entry[0x01:0x01+10] + \
                    entry[0x0e:0x0e+12] + entry[0x1c:0x1c+4] + lfn
            elif attributes & 0x08:
                # Volume label.
                pass
            elif attributes & 0x10 and shortFilename != b'.       ' and shortFilename != b'..      ':
                print('%sEntering directory . . .' % pad)
                # Create the directory structure.
                try:
                    os.mkdir('%s%s%s' % (sys.argv[2], path, filename))
                except OSError:
                    pass
                previousPosition = f.tell()
                readDirectory(cluster, tabs+1, '%s%s/' % (path, filename))
                f.seek(previousPosition)
            elif shortFilename != b'.       ' and shortFilename != b'..      ' and filesize != 0xFFFFFFFF:
                # Modify this condition if you want to export some files.
                if True:
                    continue

                # Export the files.
                previousPosition = f.tell()
                fout = open('%s%s%s' % (sys.argv[2], path, filename), 'wb')

                # For recovering files, it may be useful to assume contiguous clusters.
                contiguous = True
                if contiguous:
                    f.seek(clust2byte(cluster))
                    fout.write(f.read(filesize))
                else:
                    curCluster = cluster
                    while curCluster < 0x0FFFFFF0:
                        f.seek(clust2byte(curCluster))
                        fout.write(f.read(bytesPerCluster))
                fout.close()
                f.seek(previousPosition)
        directoryCluster = fatEntries[directoryCluster]
        if (directoryCluster & 0xFFFFFF8) == 0xFFFFFF8:
            break


def clusterChain(firstCluster, dataLength=None, noFatChain=False):
    # exFAT files flagged NoFatChain are contiguous and have no FAT entries.
    if noFatChain:
        count = (dataLength + bytesPerCluster - 1) // bytesPerCluster
        return list(range(firstCluster, firstCluster + count))
    clusters = list()
    cluster = firstCluster
    while 2 <= cluster < 0x0FFFFFF7 and len(clusters) <= totalClusters:
        clusters.append(cluster)
        cluster = fatEntries[cluster]
    return clusters


def printClusterRanges(pad, clusters):
    if not clusters:
        return
    first = last = clusters[0]
    for cluster in clusters[1:]:
        if cluster != last + 1:
            print('%sClusters: %d - %d' % (pad, first, last))
            first = cluster
        last = cluster
    print('%sClusters: %d - %d' % (pad, first, last))


def readDirectoryExFat(clusters, tabs, path='/'):
    pad = '\t' * tabs

    # Read the whole directory first: an entry set can span a cluster boundary.
    data = b''.join(readCluster(c) for c in clusters)

    i = 0
    while i + 32 <= len(data):
        entryType = data[i]
        if entryType == 0x00:
            # End of directory.
            break
        if entryType != 0x85:
            # Not an in-use File entry (bitmap, up-case table, label, deleted entry, ...).
            i += 32
            continue

        secondaryCount = data[i+1]
        attributes,     = struct.unpack("<H", data[i+4:i+6])
        lastModified,   = struct.unpack("<I", data[i+0x0C:i+0x10])
        entrySet = data[i+32:i+32+secondaryCount*32]
        i += 32 + secondaryCount*32

        if len(entrySet) < 32 or entrySet[0] != 0xC0:
            continue
        flags          = entrySet[1]
        nameLength     = entrySet[3]
        cluster,       = struct.unpack("<I", entrySet[0x14:0x18])
        filesize,      = struct.unpack("<Q", entrySet[0x18:0x20])
        noFatChain     = bool(flags & 0x02)

        name = b''
        for j in range(32, len(entrySet), 32):
            if entrySet[j] == 0xC1:
                name += entrySet[j+2:j+32]
        filename = name.decode('utf-16-le', 'replace')[:nameLength]

        print('\n%sFilename:   %s' % (pad, filename))
        print('%sAttributes: 0x%02X' % (pad, attributes))
        print('%sFilesize:   %d' % (pad, filesize))
        print('%sLast Modified: %d-%d-%d %d:%02d:%02d' % (pad,
                                                          1980 + (lastModified >> 25), (lastModified >> 21) & 0xf, (lastModified >> 16) & 0x1f,
                                                          (lastModified >> 11) & 0x1f, (lastModified >> 5) & 0x3f, (lastModified & 0x1f)*2
                                                          ))

        fileClusters = clusterChain(cluster, filesize, noFatChain) if cluster != 0 else []
        printClusterRanges(pad, fileClusters)

        if attributes & 0x10:
            print('%sEntering directory . . .' % pad)
            try:
                os.mkdir('%s%s%s' % (sys.argv[2], path, filename))
            except OSError:
                pass
            readDirectoryExFat(fileClusters, tabs+1, '%s%s/' % (path, filename))
        else:
            # Modify this condition if you want to export some files.
            if True:
                continue

            # Export the files.
            with open('%s%s%s' % (sys.argv[2], path, filename), 'wb') as fout:
                remaining = filesize
                for c in fileClusters:
                    if remaining <= 0:
                        break
                    chunk = readCluster(c)[:remaining]
                    fout.write(chunk)
                    remaining -= len(chunk)


def readCluster(cluster):
    f.seek(clust2byte(cluster))
    return f.read(bytesPerCluster)


MP4_SIGNATURE = b'\0\0\0 ftypmp42'

# Tesla 2026.20+ encrypted clips are eCryptfs-style containers (format from
# https://github.com/XGxF3/tesla-dashcam-decrypt): a header that starts with the
# plaintext size and an eCryptfs marker, then 4096-byte encrypted pages from 0x2000.
ECRYPTFS_MARKER = 0x3c81b7f5
ENC_PAGE_SIZE = 0x1000
ENC_PAYLOAD_OFFSET = 0x2000
ENC_MAX_SIZE = 4 << 30


def isEncryptedClip(header):
    if len(header) < 0x18:
        return False
    plaintextSize, magic1, magic2 = struct.unpack(">QII", header[0x00:0x10])
    pageSize, = struct.unpack(">I", header[0x14:0x18])
    return magic1 ^ magic2 == ECRYPTFS_MARKER and pageSize == ENC_PAGE_SIZE and 0 < plaintextSize < ENC_MAX_SIZE


def isClipStart(header):
    return header.startswith(MP4_SIGNATURE) or isEncryptedClip(header)


def extractMP4s(startCluster, endCluster, maxSize):
    try:
        os.mkdir('%s/mp4s' % (sys.argv[2]))
    except OSError:
        pass
    cluster = max(startCluster, 2)
    while cluster < endCluster:
        if cluster % 1000 == 0:
            print('Currently on cluster %d' % (cluster))
        f.seek(clust2byte(cluster))
        header = f.read(0x18)
        if header.startswith(MP4_SIGNATURE):
            print('Found mp4 at cluster %d' % (cluster))
            fout = open('%s/mp4s/%d.mp4' % (sys.argv[2], cluster), 'wb')
            curCluster = cluster
            bytesWritten = 0
            while True:
                f.seek(clust2byte(curCluster))
                data = f.read(bytesPerCluster)
                # Stop at the start of the next clip of either kind.
                if bytesWritten > 0 and isClipStart(data):
                    break
                fout.write(data)
                bytesWritten += bytesPerCluster
                curCluster += 1
                if bytesWritten >= maxSize:
                    break
            fout.close()
        elif isEncryptedClip(header):
            print('Found encrypted clip at cluster %d' % (cluster))
            # The header stores the plaintext size, so the exact length is known (assuming contiguous clusters).
            plaintextSize, = struct.unpack(">Q", header[0:8])
            length = ENC_PAYLOAD_OFFSET + -(-plaintextSize // ENC_PAGE_SIZE) * ENC_PAGE_SIZE
            f.seek(clust2byte(cluster))
            with open('%s/mp4s/%d-encrypted.mp4' % (sys.argv[2], cluster), 'wb') as fout:
                fout.write(f.read(length))
        cluster += 1


if mode == 'list':
    print('Reading directories...')
    if isExFat:
        readDirectoryExFat(clusterChain(rootDirectoryCluster), 1)
    else:
        readDirectory(rootDirectoryCluster, 1)

if mode == 'carve':
    print('Finding clips...')
    extractMP4s(0, totalClusters + 2, 40000000)

f.close()
