"""Local HTTP API + static frontend. Long steps run as one background job at a time;
the page polls /api/state for progress."""

import ctypes
import json
import os
import string
import threading
import time
import traceback

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, decrypt, export, scan
from .library import Library
from .telemetry import read_telemetry

STATIC = os.path.join(os.path.dirname(__file__), 'static')
SETTINGS = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'TeslaRecover', 'settings.json')


class Job:
    def __init__(self, kind, fn):
        self.kind = kind
        self.phase = 'Starting'
        self.done = 0
        self.total = 0
        self.status = 'running'
        self.error = None
        self.result = None
        self.cancel_requested = False
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def progress(self, phase, done, total):
        self.phase, self.done, self.total = phase, done, total

    def cancelled(self):
        return self.cancel_requested

    def _run(self, fn):
        try:
            self.result = fn(self)
            self.status = 'done'
        except InterruptedError:
            self.status = 'cancelled'
        except decrypt.AuthError as e:
            self.status, self.error = 'auth', str(e)
        except Exception as e:
            traceback.print_exc()
            self.status, self.error = 'error', str(e) or type(e).__name__

    def as_dict(self):
        return {'kind': self.kind, 'phase': self.phase, 'done': self.done, 'total': self.total,
                'status': self.status, 'error': self.error, 'result': self.result}


class State:
    def __init__(self):
        self.drive = None
        self.scan = None
        self.out_dir = None
        self.token = None
        self.job = None
        self.window = None
        self.library = None
        self.lock = threading.Lock()
        self.settings = {}
        try:
            with open(SETTINGS, encoding='utf-8') as f:
                self.settings = json.load(f)
        except (OSError, ValueError):
            pass

    def save_settings(self, **values):
        self.settings.update(values)
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, indent=1)

    def start(self, kind, fn):
        with self.lock:
            if self.job and self.job.status == 'running':
                raise HTTPException(409, 'Another step is still running')
            self.job = Job(kind, fn)
            self.job.thread.start()
            return self.job.as_dict()


state = State()
app = FastAPI()


@app.middleware('http')
async def no_cache_frontend(request, call_next):
    # The page and its scripts change with app updates; always revalidate them.
    response = await call_next(request)
    if not request.url.path.startswith(('/media/', '/api/')):
        response.headers['Cache-Control'] = 'no-cache'
    return response


def list_drives():
    drives = []
    if os.name != 'nt':
        return drives
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = letter + ':\\'
        kind = k32.GetDriveTypeW(root)
        if kind not in (2, 3):  # removable, fixed (some USB SSDs report as fixed)
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261):
            continue
        tesla = os.path.isdir(root + 'TeslaCam')
        if kind == 3 and not tesla:
            continue
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        drives.append({'drive': letter + ':', 'label': label.value, 'fs': fs.value, 'teslacam': tesla,
                       'removable': kind == 2, 'size': total.value,
                       'supported': fs.value.upper() in ('EXFAT', 'FAT32')})
    return drives


def pick_folder(initial=None):
    if state.window is not None:
        import webview
        result = state.window.create_file_dialog(webview.FileDialog.FOLDER, directory=initial or '')
        return result[0] if result else None
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    path = filedialog.askdirectory(initialdir=initial or None)
    root.destroy()
    return path or None


def clip_summary(c):
    return {k: c.get(k) for k in ('id', 'source', 'name', 'camera', 'folder', 'start_utc', 'size',
                                  'encrypted', 'truncated', 'status')}


@app.get('/api/state')
def get_state():
    lib = state.library
    return {
        'drive': state.drive,
        'scanned': state.scan is not None,
        'clip_count': len(state.scan['clips']) if state.scan else 0,
        'out_dir': state.out_dir,
        'last_out_dir': state.settings.get('out_dir'),
        'signed_in': state.token is not None,
        'job': state.job.as_dict() if state.job else None,
        'library': {'folder': lib.folder, 'counts': lib.summary()} if lib else None,
    }


@app.get('/api/drives')
def get_drives():
    return list_drives()


@app.post('/api/scan')
def start_scan(drive: str = Body(..., embed=True)):
    def run(job):
        result = scan.Scanner(drive, job.progress, job.cancelled).run()
        state.drive, state.scan = drive, result
        clips = result['clips']
        return {'clips': len(clips), 'live': sum(c['source'] == 'live' for c in clips),
                'deleted': sum(c['source'] != 'live' for c in clips)}
    state.scan = None
    return state.start('scan', run)


@app.get('/api/clips')
def get_clips():
    if not state.scan:
        raise HTTPException(404, 'Scan the drive first')
    return [clip_summary(c) for c in state.scan['clips']]


@app.post('/api/pick-folder')
def post_pick_folder(initial: str = Body(None, embed=True)):
    return {'path': pick_folder(initial or state.settings.get('out_dir'))}


@app.post('/api/export')
def start_export(out_dir: str = Body(...), start_utc: float = Body(None), end_utc: float = Body(None)):
    if not state.scan:
        raise HTTPException(400, 'Scan the drive first')
    clips = export.select(state.scan['clips'], start_utc, end_utc)
    if not clips:
        raise HTTPException(400, 'No clips in the selected range')
    state.out_dir = out_dir
    state.save_settings(out_dir=out_dir)

    def run(job):
        state.library = export.export(state.drive, state.scan, clips, out_dir, job.progress, job.cancelled)
        return {'copied': len(clips), 'size': sum(c['size'] for c in clips)}
    return state.start('export', run)


def sample_clip():
    """An encrypted clip for the sign-in page to open (see auth.sign_in)."""
    lib = state.library
    if lib:
        for e in lib.entries.values():
            path = os.path.join(lib.folder, 'encrypted', e.get('encrypted_file') or '')
            if e.get('encrypted_file') and os.path.isfile(path):
                return path
    if state.scan:
        for c in state.scan['clips']:
            path = state.drive.rstrip('\\/') + (c['path'] or '').replace('/', os.sep)
            if c['source'] == 'live' and c['encrypted'] and os.path.isfile(path):
                return path
    return None


@app.post('/api/signin')
def start_signin():
    def run(job):
        state.token = auth.sign_in(job.progress, job.cancelled, sample_clip=sample_clip())
        return {'signed_in': True}
    return state.start('signin', run)


@app.post('/api/token')
def set_token(token: str = Body(..., embed=True)):
    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if not token:
        raise HTTPException(400, 'Empty token')
    state.token = token
    return {'signed_in': True}


@app.post('/api/decrypt')
def start_decrypt(keep_encrypted: bool = Body(True, embed=True)):
    lib = state.library
    if lib is None:
        raise HTTPException(400, 'Copy clips to an output folder first')
    needs_key = any(e.get('status') in ('encrypted', 'key_error') for e in lib.entries.values())
    if needs_key and not state.token:
        raise HTTPException(400, 'Sign in to Tesla first')

    def run(job):
        decrypt.prepare_plain(lib)
        try:
            if needs_key:
                decrypt.decrypt_all(lib, state.token, job.progress, job.cancelled)
        except decrypt.AuthError:
            state.token = None
            raise
        finally:
            # Rename whatever was decrypted, even if a later batch failed.
            decrypt.finalize(lib, job.progress, keep_encrypted)
        return lib.summary()
    return state.start('decrypt', run)


@app.post('/api/repair')
def start_repair():
    lib = state.library
    if lib is None:
        raise HTTPException(400, 'No library open')
    if lib.read_only:
        raise HTTPException(400, 'Clips on the Tesla drive itself are never changed')

    def run(job):
        decrypt.repair_unplayable(lib, job.progress)
        return lib.summary()
    return state.start('repair', run)


@app.post('/api/open-library')
def open_library(folder: str = Body(..., embed=True)):
    if not os.path.isdir(folder):
        raise HTTPException(404, 'Folder not found')
    root = os.path.splitdrive(os.path.abspath(folder))[0] + os.sep
    lib = Library(folder, read_only=os.path.isdir(os.path.join(root, 'TeslaCam')))
    added = lib.index_folder()
    if added:
        lib.save()
    state.library = lib
    state.out_dir = lib.folder
    return {'folder': lib.folder, 'counts': lib.summary()}


@app.get('/api/library')
def get_library():
    lib = state.library
    if lib is None:
        raise HTTPException(404, 'No library open')
    clips = [{k: e.get(k) for k in ('id', 'file', 'camera', 'recorded_utc', 'title', 'comment', 'source', 'width')}
             for e in lib.playable()]
    clips.sort(key=lambda c: (c['recorded_utc'] or 0, c['camera'] or ''))
    unplayable = [{k: e.get(k) for k in ('id', 'file', 'camera', 'start_utc', 'error')}
                  for e in lib.entries.values() if e.get('status') != 'ok']
    return {'folder': lib.folder, 'clips': clips, 'other': unplayable}


def _library_file(name):
    lib = state.library
    if lib is None:
        raise HTTPException(404, 'No library open')
    path = os.path.abspath(os.path.join(lib.folder, name))
    if not path.startswith(lib.folder + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, 'Not found')
    return path


@app.get('/media/{name:path}')
def media(name: str):
    return FileResponse(_library_file(name), media_type='video/mp4')


@app.get('/api/telemetry')
def telemetry(file: str):
    path = _library_file(file)
    cache_dir = os.path.join(state.library.folder, '.telemetry')
    cache = os.path.join(cache_dir, os.path.basename(file) + '.json')
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        return FileResponse(cache, media_type='application/json')
    try:
        data = read_telemetry(path)
    except Exception as e:
        raise HTTPException(422, 'No telemetry: %s' % e)
    if not state.library.read_only:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
    return JSONResponse(data)


@app.post('/api/save')
def start_save(files: list[str] = Body(...), dest: str = Body(...)):
    for name in files:
        _library_file(name)
    return state.start('save', lambda job: export.save_files(state.library.folder, files, dest, job.progress))


@app.post('/api/cancel')
def cancel():
    if state.job:
        state.job.cancel_requested = True
    return {'ok': True}


@app.post('/api/open-folder')
def open_folder(folder: str = Body(None, embed=True)):
    folder = folder or (state.library.folder if state.library else None)
    if folder and os.path.isdir(folder):
        os.startfile(folder)
    return {'ok': True}


app.mount('/', StaticFiles(directory=STATIC, html=True), name='static')
