"""Let the user sign in to dashcam.tesla.com and pick up the Bearer token the site uses for its API.

Tesla's sign-in page has bot protection that rejects automated browsers (the captcha never
passes), so the sign-in happens in an ordinary, unautomated Edge/Chrome window with its own
profile. While the user signs in, the app only watches the window's address through the local
debugging endpoint (/json/list), without attaching to the page. Once the window is back on
dashcam.tesla.com, Playwright connects over CDP and reads the token from the site's API requests
or the OAuth token response. The token is only kept in memory.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

DASHCAM_URL = 'https://dashcam.tesla.com'
JWT_RE = re.compile(r'^eyJ[\w-]+\.[\w-]+\.[\w-]+$')
PROFILE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'TeslaRecover', 'browser-profile')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class SignInClosed(RuntimeError):
    pass


def find_browser():
    """An installed Edge or Chrome (Edge ships with Windows 11)."""
    candidates = []
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
        if base:
            candidates += [os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
                           os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe')]
    for name in ('msedge', 'chrome'):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return next((c for c in candidates if os.path.isfile(c)), None)


def install_chromium(progress):
    """Fallback when neither Edge nor Chrome is installed: download Playwright's Chromium, with progress."""
    phase = 'Downloading the sign-in browser (one time)'
    progress(phase, 0, 100)
    proc = subprocess.Popen([sys.executable, '-m', 'playwright', 'install', 'chromium'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    output = b''
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, 'read1') else proc.stdout.read(256)
        if not chunk:
            break
        output = (output + chunk)[-4000:]
        # Lines look like "|■■■■■■■■      |  30% of 172.5 MiB".
        found = re.findall(rb'(\d+)% of ([\d.]+ \w+)', output)
        if found:
            pct, size = found[-1]
            progress('%s — %s' % (phase, size.decode()), int(pct), 100)
    if proc.wait() != 0:
        raise RuntimeError('Downloading the sign-in browser failed: ' + output.decode('utf-8', 'replace')[-300:])
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _targets(port):
    with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % port, timeout=2) as r:
        return json.load(r)


def _token_from_storage(page):
    """An access token the site kept in local/session storage or a cookie."""
    found = page.evaluate('''() => {
        const out = [];
        for (const s of [localStorage, sessionStorage])
            for (let i = 0; i < s.length; i++) out.push([s.key(i), s.getItem(s.key(i))]);
        return out;
    }''')
    for key, value in found:
        if 'access' in key.lower() and value and JWT_RE.match(value.strip('"')):
            return value.strip('"')
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            for k in ('access_token', 'accessToken'):
                if isinstance(data.get(k), str):
                    return data[k]
    for cookie in page.context.cookies(DASHCAM_URL):
        if cookie['name'] in ('token', 'access_token'):
            return cookie['value']
    return None


def sign_in(progress, cancelled=lambda: False, timeout=900, sample_clip=None):
    """sample_clip: an encrypted clip to load into the dashcam page if the site makes no API
    call on its own (it only asks Tesla for keys once a clip is opened)."""
    exe = find_browser() or install_chromium(progress)
    port = _free_port()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    progress('Opening the Tesla sign-in window', 0, 1)
    proc = subprocess.Popen([exe, '--remote-debugging-port=%d' % port, '--user-data-dir=' + PROFILE_DIR,
                             '--no-first-run', '--no-default-browser-check', '--new-window', DASHCAM_URL])
    try:
        _wait_until_signed_in(proc, port, progress, cancelled, timeout)
        return _capture_token(proc, port, progress, cancelled, sample_clip)
    finally:
        _close_browser(proc, port)


def _open_clip(page, path):
    """Give the dashcam page a clip, like choosing one in its file picker. The page then asks
    Tesla's API for the clip's key, using the token we are waiting for."""
    try:
        page.wait_for_load_state('networkidle', timeout=10000)
    except Exception:
        pass
    for frame in page.frames:
        inputs = frame.locator('input[type=file]')
        try:
            if inputs.count():
                inputs.first.set_input_files(path)
                return True
        except Exception:
            continue
    # No file input in the DOM: click the page's upload/select button and answer its file chooser.
    button = page.get_by_role('button', name=re.compile(r'upload|select|choose|browse|open|add', re.I))
    try:
        if button.count():
            with page.expect_file_chooser(timeout=5000) as chooser:
                button.first.click()
            chooser.value.set_files(path)
            return True
    except Exception:
        pass
    return False


def _close_browser(proc, port):
    """Close the sign-in window (Browser.close over CDP, then the process if it is still there)."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % port, timeout=2) as r:
            ws_url = json.load(r)['webSocketDebuggerUrl']
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            browser.new_browser_cdp_session().send('Browser.close')
    except Exception:
        pass
    try:
        proc.terminate()
    except OSError:
        pass


def _alive(port):
    try:
        _targets(port)
        return True
    except OSError:
        return False


def _check(proc, port, started, timeout, cancelled):
    if cancelled():
        raise InterruptedError('cancelled')
    if time.time() - started > timeout:
        raise TimeoutError('Sign-in took too long')
    # The launcher process may hand over to another one, so also ask the debugging endpoint.
    if proc.poll() is not None and not _alive(port):
        raise SignInClosed('The sign-in window was closed before signing in finished')


def _wait_until_signed_in(proc, port, progress, cancelled, timeout):
    """Watch the window's address without attaching to it until it is on dashcam.tesla.com."""
    progress('Waiting for you to sign in in the Tesla window', 0, 1)
    started = time.time()
    seen_login = False
    on_site_since = None
    while True:
        time.sleep(1)
        _check(proc, port, started, timeout, cancelled)
        try:
            urls = [t['url'] for t in _targets(port) if t.get('type') == 'page']
        except OSError:
            continue  # browser still starting
        if any('auth.tesla.com' in u for u in urls):
            seen_login = True
            on_site_since = None
            continue
        on_site = any(u.startswith(DASHCAM_URL) and '/callback' not in u for u in urls)
        if not on_site:
            on_site_since = None
            continue
        on_site_since = on_site_since or time.time()
        # Already signed in from an earlier session (no login page), or back from the login page:
        # give the site a moment to finish its own token exchange.
        if time.time() - on_site_since > (3 if seen_login else 6):
            return


def _capture_token(proc, port, progress, cancelled, sample_clip=None):
    from playwright.sync_api import sync_playwright

    token = {}

    def on_request(request):
        auth = request.headers.get('authorization', '')
        if auth.lower().startswith('bearer ') and 'tesla.com' in request.url:
            token.setdefault('value', auth[7:].strip())

    def on_response(response):
        if '/oauth2/v3/token' in response.url and response.ok:
            try:
                value = response.json().get('access_token')
            except Exception:
                return
            if value:
                token.setdefault('value', value)

    progress('Reading your Tesla sign-in', 0, 1)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://127.0.0.1:%d' % port)
        try:
            pages = []
            for context in browser.contexts:
                context.on('request', on_request)
                context.on('response', on_response)
                pages += [pg for pg in context.pages if pg.url.startswith(DASHCAM_URL)]
            if not pages:
                raise SignInClosed('The dashcam.tesla.com tab was closed')
            page = pages[0]
            try:
                value = _token_from_storage(page)
                if value:
                    token.setdefault('value', value)
            except Exception:
                pass
            if 'value' not in token:
                # Reloading makes the site call its API (or redo the token exchange) while we listen.
                page.reload()
            started = time.time()
            uploaded = False
            while 'value' not in token:
                _check(proc, port, started, 90, cancelled)
                page.wait_for_timeout(500)
                if not uploaded and time.time() - started > 4:
                    uploaded = True
                    if sample_clip and _open_clip(page, sample_clip):
                        progress('Opened one of your clips on dashcam.tesla.com to finish signing in', 0, 1)
                    else:
                        progress('Almost done: open any encrypted clip on the dashcam.tesla.com page', 0, 1)
                if int(time.time() - started) % 5 == 4:
                    try:
                        value = _token_from_storage(page)
                        if value:
                            token.setdefault('value', value)
                    except Exception:
                        pass
            progress('Signed in', 1, 1)
            return token['value']
        finally:
            try:
                browser.close()
            except Exception:
                pass
