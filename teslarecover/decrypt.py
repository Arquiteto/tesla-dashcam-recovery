"""Decrypt the exported clips with keys from dashcam.tesla.com, then give them their final names."""

import datetime
import os
import shutil

import sys

import requests

# tesla_dashcam_decrypt.py lives next to the package (a copy of the upstream tool).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tesla_dashcam_decrypt as tdd  # noqa: E402

from . import repair, vision
from .naming import FRONT_WIDTH, HW4_SIDE_WIDTH, assignCameras, clipName, readMetadata
from .scan import parse_name

BATCH_SIZE = 20
UNPLAYABLE_DIR = 'unplayable'


class AuthError(Exception):
    pass


def decrypt_all(lib, token, progress, cancelled=lambda: False):
    """Decrypt every 'encrypted' entry into <out>/<id>.raw.mp4 (status 'raw')."""
    pending = [e for e in lib.entries.values() if e.get('status') in ('encrypted', 'key_error')]
    session = tdd.get_session(token)
    done = 0
    for start in range(0, len(pending), BATCH_SIZE):
        if cancelled():
            raise InterruptedError('cancelled')
        batch = pending[start:start + BATCH_SIZE]
        headers = {}
        for entry in batch:
            src = os.path.join(lib.folder, 'encrypted', entry['encrypted_file'])
            try:
                header = tdd.read_file_header(src)
                headers[header['id']] = (header, entry, src)
            except Exception as e:
                entry.update(status='unplayable', error="can't read header: %s" % e)
        progress('Getting keys from Tesla', done, len(pending))
        try:
            keys = tdd.fetch_keys_batch(session, [h for h, _, _ in headers.values()]) if headers else {}
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                lib.save()
                raise AuthError('Tesla sign-in expired or was refused - please sign in again') from None
            raise
        for uid, (header, entry, src) in headers.items():
            progress('Decrypting', done, len(pending))
            if uid not in keys:
                entry.update(status='key_error', error='Tesla did not return a key for this clip')
                continue
            raw = os.path.join(lib.folder, '%s.raw.mp4' % entry['id'])
            try:
                tdd._decrypt_real_file(src, raw + '.part', keys[uid])
                os.replace(raw + '.part', raw)
                entry.update(status='raw', file=os.path.basename(raw), error=None)
            except Exception as e:
                entry.update(status='unplayable', error='decryption failed: %s' % e)
                if os.path.exists(raw + '.part'):
                    os.remove(raw + '.part')
            done += 1
        done = start + len(batch)
        lib.save()
    progress('Decrypting', len(pending), len(pending))


def prepare_plain(lib):
    """Unencrypted clips (older firmware) only need moving into place."""
    for entry in lib.entries.values():
        if entry.get('status') == 'plain':
            src = os.path.join(lib.folder, 'encrypted', entry['encrypted_file'])
            raw = os.path.join(lib.folder, '%s.raw.mp4' % entry['id'])
            shutil.move(src, raw)
            entry.update(status='raw', file=os.path.basename(raw))


def finalize(lib, progress, keep_encrypted=True):
    """Read MP4 metadata, work out cameras and rename 'raw' entries to their final names."""
    raw = [e for e in lib.entries.values() if e.get('status') == 'raw']
    clips = []
    for i, entry in enumerate(raw):
        progress('Reading clip details', i, len(raw))
        path = os.path.join(lib.folder, entry['file'])
        try:
            recorded, width, title, comment = readMetadata(path)
        except Exception as e:
            # Usually a deleted clip that was fragmented on disk, so the copied bytes are incomplete
            # and the index at the end of the file (moov) is gone. Rebuild it from the frames.
            if _try_repair(lib, entry, path):
                continue
            if entry['file'].startswith(UNPLAYABLE_DIR + '/'):
                entry.update(status='unplayable', error=str(e))
            else:
                _move_unplayable(lib, entry, str(e))
            continue
        entry.update(recorded_utc=recorded.timestamp(), width=width, title=title, comment=comment)
    _align_repaired(lib)

    # Cameras: Tesla's name when known, otherwise width and cluster adjacency (see naming.py).
    for entry in lib.entries.values():
        if entry.get('recorded_utc') is None or not entry.get('runs'):
            continue
        last_start, last_count = entry['runs'][-1]
        clips.append({'cluster': entry['runs'][0][0], 'end_cluster': last_start + last_count,
                      'size': entry.get('size', 0), 'width': entry.get('width', 0),
                      'recorded': entry['recorded_utc'], 'entry': entry})
    listed = {c['cluster']: parse_name(c['entry']['name'])[1] for c in clips if c['entry'].get('name')}
    listed = {k: v for k, v in listed.items() if v}
    for c in clips:
        # Known cameras of clips done earlier anchor the chain (but not 'side' = unknown).
        if c['entry'].get('status') == 'ok' and c['entry'].get('camera') not in (None, 'side'):
            listed.setdefault(c['cluster'], c['entry']['camera'])
    assignCameras(clips, listed, lib.cluster_size)
    _check_hw3_back_camera(lib, clips, listed, progress)

    taken = {e['file'] for e in lib.entries.values() if e.get('status') == 'ok'}
    for c in clips:
        entry = c['entry']
        relabel = (entry.get('status') == 'ok' and entry.get('camera') != c['camera']
                   and entry.get('source') != 'folder')
        if entry.get('status') != 'raw' and not relabel:
            continue
        if relabel:
            # New information about this clip's camera (newly decrypted or repaired clips completed
            # the chain of adjacent files, or the picture check corrected it): rename it.
            taken.discard(entry['file'])
            entry['status'] = 'raw'
        entry['camera'] = c['camera']
        recorded = datetime.datetime.fromtimestamp(entry['recorded_utc'], datetime.timezone.utc)
        extra = (entry['title'], entry['comment'], 'repaired' if entry.get('repaired') else '')
        name = _unique(taken, lambda s: clipName(recorded, c['camera'], extra, suffix=s))
        os.replace(os.path.join(lib.folder, entry['file']), os.path.join(lib.folder, name))
        # Also set the file's modified time so Explorer sorts by recording time.
        os.utime(os.path.join(lib.folder, name), (entry['recorded_utc'], entry['recorded_utc']))
        entry.update(file=name, status='ok', start_utc=entry['recorded_utc'])
        if not keep_encrypted and entry.get('encrypted_file'):
            enc = os.path.join(lib.folder, 'encrypted', entry['encrypted_file'])
            if os.path.exists(enc):
                os.remove(enc)
    lib.save()
    progress('Done', len(raw), len(raw))


def _check_hw3_back_camera(lib, clips, listed, progress):
    """HW3 cameras are only told apart by the order the car writes them in, which is occasionally
    different. The back camera's fisheye picture is recognisable, so check it on complete HW3
    minutes that have new clips. If the picture clearly contradicts the order, the order was wrong:
    the fisheye clip becomes the back camera, and the other inferred cameras become 'side'
    (unknown), since a wrong name is worse than none."""
    groups = {}
    for c in clips:
        groups.setdefault(c['recorded'], []).append(c)
    todo = [g for g in groups.values()
            if len(g) == 4 and any(c['entry'].get('status') == 'raw' for c in g)
            and not any(c['width'] in (FRONT_WIDTH, HW4_SIDE_WIDTH) for c in g)
            and not all(c['cluster'] in listed for c in g)]
    for i, group in enumerate(todo):
        progress('Checking camera positions', i, len(todo))
        back = vision.find_back_camera([os.path.join(lib.folder, c['entry']['file']) for c in group])
        if back is None or group[back]['camera'] == 'back':
            continue
        for j, c in enumerate(group):
            if j == back:
                c['camera'] = 'back'
            elif c['cluster'] not in listed:
                c['camera'] = 'side'


def _try_repair(lib, entry, path):
    if not entry.get('start_utc') or not os.path.exists(path):
        return False
    dst = os.path.join(lib.folder, '%s.repaired.mp4' % entry['id'])
    try:
        info = repair.repair(path, dst + '.part', entry['start_utc'])
    except Exception as e:
        entry['repair_error'] = str(e)
        if os.path.exists(dst + '.part'):
            os.remove(dst + '.part')
        return False
    os.replace(dst + '.part', dst)
    os.remove(path)
    entry.update(file=os.path.basename(dst), status='raw', recorded_utc=entry['start_utc'], width=info['width'],
                 title='', comment='', repaired=True, repaired_seconds=round(info['seconds'], 1), error=None)
    return True


def _align_repaired(lib):
    """A repaired clip's start is only estimated from its header (which records when the clip
    ended). The other cameras of the same minute ended within a few seconds of it: take their
    exact start and location so it lands in the same minute and camera group."""
    known = [e for e in lib.entries.values()
             if e.get('end_utc') and e.get('recorded_utc') and not e.get('repaired') and e.get('status') in ('ok', 'raw')]
    for entry in lib.entries.values():
        if not entry.get('repaired') or entry.get('status') != 'raw' or not entry.get('end_utc'):
            continue
        best = min(known, key=lambda k: abs(k['end_utc'] - entry['end_utc']), default=None)
        if best and abs(best['end_utc'] - entry['end_utc']) <= 5:
            entry.update(recorded_utc=best['recorded_utc'], title=best.get('title', ''), comment=best.get('comment', ''))


def repair_unplayable(lib, progress):
    """Give clips that were left unplayable (by an older version, or before a fix) another go."""
    for entry in lib.entries.values():
        if entry.get('status') == 'unplayable' and entry.get('file') and                 os.path.exists(os.path.join(lib.folder, entry['file'])):
            entry['status'] = 'raw'
    finalize(lib, progress)


def _unique(taken, make):
    name = make('')
    n = 2
    while name in taken:
        name = make('_%d' % n)
        n += 1
    taken.add(name)
    return name


def _move_unplayable(lib, entry, error):
    folder = os.path.join(lib.folder, UNPLAYABLE_DIR)
    os.makedirs(folder, exist_ok=True)
    taken = set(os.listdir(folder))
    camera = entry.get('camera') or 'unknown'
    if entry.get('start_utc'):
        start = datetime.datetime.fromtimestamp(entry['start_utc'], datetime.timezone.utc)
        name = _unique(taken, lambda s: clipName(start, camera, (entry['id'],), suffix=s))
    else:
        name = _unique(taken, lambda s: '%s%s.mp4' % (entry['id'], s))
    os.replace(os.path.join(lib.folder, entry['file']), os.path.join(folder, name))
    entry.update(status='unplayable', file=UNPLAYABLE_DIR + '/' + name, error=error)
