"""<out>/library.json: one entry per recovered clip, from scan to decrypted file."""

import datetime
import json
import os
import re

from .naming import CAMERA_ORDER, assignCameras, readMetadata
from .scan import parse_name

LIBRARY_FILE = 'library.json'
CAMERA_IN_NAME = re.compile(r'\d\d-\d\d-\d\d-(%s)(?=[_.-])' % '|'.join(CAMERA_ORDER + ('side',)))


class Library:
    def __init__(self, folder, read_only=False):
        self.folder = os.path.abspath(folder)
        # Read-only: a folder on the Tesla drive itself, which must never be written to.
        self.read_only = read_only
        self.path = os.path.join(self.folder, LIBRARY_FILE)
        self.cluster_size = 131072
        self.entries = {}
        if os.path.exists(self.path):
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            self.cluster_size = data.get('cluster_size', self.cluster_size)
            self.entries = {e['id']: e for e in data['clips']}

    def get(self, clip_id):
        return self.entries.get(clip_id)

    def upsert(self, clip, **fields):
        entry = self.entries.setdefault(clip['id'], dict(clip))
        entry.update(fields)
        return entry

    def save(self):
        if self.read_only:
            return
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'cluster_size': self.cluster_size,
                       'clips': sorted(self.entries.values(), key=lambda e: (e.get('start_utc') or 0, e['id']))},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def index_folder(self):
        """Add playable MP4s that are in the folder but not in the library (e.g. a folder of
        clips copied from the drive in Explorer, or the output of the old scripts)."""
        known = {e.get('file') for e in self.entries.values()}
        added = []
        for name in sorted(os.listdir(self.folder)):
            if not name.lower().endswith('.mp4') or name in known:
                continue
            path = os.path.join(self.folder, name)
            try:
                recorded, width, title, comment = readMetadata(path)
            except Exception:
                continue
            _, camera = parse_name(name)
            m = re.match(r'(\d+)(?:-encrypted)?\.mp4$', name)
            if camera is None:
                m2 = CAMERA_IN_NAME.search(name)
                camera = m2.group(1) if m2 else None
            entry = self.entries['file:' + name] = {
                'id': 'file:' + name, 'source': 'folder', 'name': name, 'camera': camera,
                'runs': [[int(m.group(1)), 1]] if m else [], 'start_utc': recorded.timestamp(),
                'recorded_utc': recorded.timestamp(), 'width': width, 'title': title, 'comment': comment,
                'file': name, 'status': 'ok', 'size': os.path.getsize(path),
            }
            added.append(entry)
        # Carved files named <cluster>.mp4: work out the camera from cluster adjacency.
        unknown = [e for e in added if e['camera'] is None]
        if unknown:
            clips = [{'cluster': e['runs'][0][0] if e['runs'] else -1 - i, 'size': e['size'], 'width': e['width'],
                      'recorded': e['recorded_utc'], 'entry': e} for i, e in enumerate(added)]
            listed = {c['cluster']: c['entry']['camera'] for c in clips if c['entry']['camera']}
            assignCameras(clips, listed, self.cluster_size)
            for c in clips:
                c['entry']['camera'] = c['camera']
        return len(added)

    def playable(self):
        return [e for e in self.entries.values() if e.get('status') == 'ok' and e.get('file')]

    def summary(self):
        counts = {}
        for e in self.entries.values():
            counts[e.get('status', '?')] = counts.get(e.get('status', '?'), 0) + 1
        return counts


def utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
