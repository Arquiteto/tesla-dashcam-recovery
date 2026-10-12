"""Start the app: local server on a free port plus a native window (pywebview).

    python -m teslarecover [--library FOLDER] [--browser] [--port N]
"""

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import uvicorn

# `uv run teslarecover` / `python teslarecover` run this file as a script, not as part of the
# package; put the repo root on the path so the package import works either way.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from teslarecover import server  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def relaunch_as_admin():
    """Raw access to a drive is sometimes refused without admin rights; restart elevated."""
    args = subprocess.list2cmdline(['-m', 'teslarecover'] + sys.argv[1:])
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, args, cwd, 1) > 32


@server.app.post('/api/elevate')
def elevate():
    if relaunch_as_admin():
        threading.Timer(1.0, lambda: os._exit(0)).start()
        return {'ok': True}
    return {'ok': False}


def main():
    parser = argparse.ArgumentParser(description='Tesla dashcam recovery')
    parser.add_argument('--library', help='open this folder of recovered clips in the viewer')
    parser.add_argument('--browser', action='store_true', help='use the default web browser instead of a window')
    parser.add_argument('--serve-only', action='store_true', help='only run the server (for development)')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()

    if args.library:
        server.open_library(args.library)
    port = args.port or free_port()
    url = 'http://127.0.0.1:%d/' % port + ('#viewer' if args.library else '')
    config = uvicorn.Config(server.app, host='127.0.0.1', port=port, log_level='warning')
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)

    if args.browser or args.serve_only:
        print('Tesla Recover running at', url, flush=True)
        if not args.serve_only:
            webbrowser.open(url)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return

    import webview
    server.state.window = webview.create_window('Tesla Dashcam Recovery', url, width=1440, height=920,
                                                min_size=(900, 600))
    webview.start()
    srv.should_exit = True


if __name__ == '__main__':
    main()
