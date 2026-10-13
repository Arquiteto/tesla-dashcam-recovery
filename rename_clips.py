#!/usr/bin/env python3
"""Rename decrypted Tesla clips using the metadata left in the MP4 header.

    <local recording start>-<camera>_<original name>_<title>_<comment>.mp4
    e.g. 2026-10-11_16-15-43-front_100690_Downtown_Main-Street.mp4

The recording start comes from moov/mvhd creation_time, which Tesla stores
in UTC; it is converted to this computer's local time zone (the same local
time Tesla uses in its own file names). Tesla puts the area in the title tag
and the street in the comment tag.

Camera: on HW4 the front camera is the only one recorded at 2896x1876 (HW3 has four
cameras, all the same size). The car
writes each minute's files back to back in camera order, so a clip that
starts on the cluster after another one ends is the next camera. With --listing (the output
of `run.py D: outdir list`) clips that still have a directory entry get their
exact Tesla name from it.

Usage: rename_clips.py folder [--listing listing.txt] [--apply] [--tz-offset HOURS]
Without --apply it only prints what it would do.
"""

import argparse
import datetime
import os
import re
import struct
import time

from teslarecover.naming import INVALID_CHARS, assignCameras, readListing, readMetadata


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder')
    parser.add_argument('--listing', help='output of `run.py D: outdir list`, for exact camera names')
    parser.add_argument('--apply', action='store_true', help='actually rename the files')
    parser.add_argument('--cluster-size', type=int, default=131072,
                        help='drive cluster size in bytes (bytes per sector x sectors per cluster from run.py)')
    parser.add_argument('--tz-offset', type=float,
                        help='use UTC plus this many hours instead of the local time zone')
    args = parser.parse_args()

    listed = readListing(args.listing) if args.listing else dict()

    clips = list()
    skipped = 0
    for name in sorted(os.listdir(args.folder)):
        # Only touch files that haven't been renamed yet.
        match = re.fullmatch(r'(\d+)(?:-encrypted)?\.mp4', name, re.IGNORECASE)
        if not match:
            continue
        path = os.path.join(args.folder, name)
        if time.time() - os.path.getmtime(path) < 60:
            print('Skipping %s: still being written' % name)
            skipped += 1
            continue
        try:
            recorded, width, title, comment = readMetadata(path)
        except (ValueError, struct.error) as e:
            # Usually a clip that was fragmented on disk, so the carved bytes are incomplete.
            print('Skipping %s: %s' % (name, e))
            skipped += 1
            continue
        clips.append({'name': name, 'path': path, 'cluster': int(match.group(1)), 'size': os.path.getsize(path),
                      'recorded': recorded, 'width': width, 'title': title, 'comment': comment})

    assignCameras(clips, listed, args.cluster_size)

    renamed = 0
    for clip in clips:
        if args.tz_offset is None:
            local = clip['recorded'].astimezone()
        else:
            local = clip['recorded'] + datetime.timedelta(hours=args.tz_offset)
        parts = ['%s-%s' % (local.strftime('%Y-%m-%d_%H-%M-%S'), clip['camera']), str(clip['cluster'])]
        parts += [p for p in (clip['title'], clip['comment']) if p]
        newName = INVALID_CHARS.sub('_', '_'.join(parts)) + '.mp4'
        newPath = os.path.join(args.folder, newName)
        if os.path.exists(newPath):
            print('Skipping %s: %s already exists' % (clip['name'], newName))
            skipped += 1
            continue

        print('%s -> %s' % (clip['name'], newName))
        if args.apply:
            os.rename(clip['path'], newPath)
            # Also set the file's modified time so Explorer sorts by recording time.
            stamp = clip['recorded'].timestamp()
            os.utime(newPath, (stamp, stamp))
        renamed += 1

    print('\n%d %s, %d skipped%s' % (renamed, 'renamed' if args.apply else 'to rename', skipped,
                                       '' if args.apply else ' (dry run; add --apply to rename)'))


if __name__ == '__main__':
    main()
