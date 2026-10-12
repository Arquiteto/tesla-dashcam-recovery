"""Copy the selected clips (still encrypted) from the drive into <out>/encrypted/."""

import json
import os
import shutil

from .library import Library
from .scan import CLIP_SECONDS
from .volume import Volume, from_runs


def select(clips, start_utc=None, end_utc=None):
    """Clips overlapping [start_utc, end_utc]; everything when no range is given."""
    if start_utc is None and end_utc is None:
        return list(clips)
    out = []
    for c in clips:
        if c['start_utc'] is None:
            continue
        if (end_utc is None or c['start_utc'] <= end_utc) and \
                (start_utc is None or c['start_utc'] + CLIP_SECONDS >= start_utc):
            out.append(c)
    return out


def encrypted_name(clip):
    # Keep Tesla's name when there is one; the cluster keeps names unique.
    return '%s_%s' % (clip['id'], clip['name']) if clip['name'] else '%s.mp4' % clip['id']


def export(drive, scan, clips, out_dir, progress, cancelled=lambda: False):
    lib = Library(out_dir)
    lib.cluster_size = scan['cluster_size']
    enc_dir = os.path.join(out_dir, 'encrypted')
    os.makedirs(enc_dir, exist_ok=True)
    total = sum(c['size'] for c in clips)
    done = 0
    vol = None
    try:
        for clip in clips:
            if cancelled():
                raise InterruptedError('cancelled')
            entry = lib.get(clip['id'])
            if entry and entry.get('status') in ('decrypted', 'unplayable', 'copied'):
                done += clip['size']
                continue
            name = encrypted_name(clip)
            dest = os.path.join(enc_dir, name)
            progress('Copying %s' % (clip['name'] or 'deleted clip %s' % clip['id']), done, total)
            if clip['source'] == 'live':
                # Through the file system, so fragmented files come out intact.
                src = drive.rstrip('\\/') + clip['path'].replace('/', '\\')
                shutil.copyfile(src, dest + '.part')
            else:
                if vol is None:
                    vol = Volume(drive)
                with open(dest + '.part', 'wb') as f:
                    written = 0
                    for chunk in vol.read_clusters(from_runs(clip['runs']), clip['size']):
                        f.write(chunk)
                        written += len(chunk)
                    # Truncated clips: pad so decryption still runs; the missing part stays broken.
                    if written < clip['size']:
                        f.write(b'\0' * (clip['size'] - written))
            os.replace(dest + '.part', dest)
            done += clip['size']
            lib.upsert(clip, encrypted_file=name, status='encrypted' if clip['encrypted'] else 'plain')
        lib.save()
        progress('Copied', total, total)
    finally:
        lib.save()
        if vol:
            vol.close()
    return lib


def save_files(out_dir, files, dest_dir, progress):
    """Copy library files (relative names) to another folder."""
    os.makedirs(dest_dir, exist_ok=True)
    for i, name in enumerate(files):
        progress('Saving %s' % name, i, len(files))
        src = os.path.join(out_dir, name)
        shutil.copy2(src, os.path.join(dest_dir, os.path.basename(name)))
    progress('Saved', len(files), len(files))
    return {'saved': len(files), 'dest': dest_dir}


if __name__ == '__main__':
    import sys
    scan = json.load(open(sys.argv[2], encoding='utf-8'))
    export(sys.argv[1], scan, scan['clips'][:int(sys.argv[4])], sys.argv[3], lambda *a: print(*a))
