"""Minimal polite Jenkins client: Basic auth from the environment, backoff, streaming to disk."""

import base64
import gzip
import http.client
import json
import os
import shutil
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SERVERS = {
    'ci': ('https://ci.ros2.org', 'JENKINS_CI_USER', 'JENKINS_CI_TOKEN'),
    'build': ('https://build.ros2.org', 'JENKINS_BUILD_USER', 'JENKINS_BUILD_TOKEN'),
}

MIN_INTERVAL = 2.0
MAX_ATTEMPTS = 6


class Jenkins:

    def __init__(self, server):
        self.base, user_var, token_var = SERVERS[server]
        user, token = os.environ.get(user_var), os.environ.get(token_var)
        if not (user and token) and os.name == 'nt':
            import credentials
            user, token = credentials.load(server) or (None, None)
        if not (user and token):
            raise SystemExit(f'no credentials: run "python credentials.py set {server}" '
                             f'or set {user_var} and {token_var}')
        self._auth = 'Basic ' + base64.b64encode(f'{user}:{token}'.encode()).decode()
        self._last = 0.0

    def _open(self, path):
        url = self.base + path
        for attempt in range(MAX_ATTEMPTS):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            request = urllib.request.Request(url, headers={'Authorization': self._auth})
            try:
                return urllib.request.urlopen(request, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS - 1:
                    delay = int(e.headers.get('Retry-After') or 0) or 10 * 2 ** attempt
                    print(f'  {e.code} for {path[:60]}, retrying in {delay}s')
                    time.sleep(delay)
                    continue
                raise
        return None

    def _retrying(self, path, read):
        """Open path and call read(response); retry the whole request if the connection drops."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._open(path)
                if response is None:
                    return None
                with response:
                    return read(response)
            except (ConnectionError, TimeoutError, http.client.HTTPException,
                    urllib.error.URLError, ssl.SSLError) as e:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = 10 * 2 ** attempt
                print(f'  connection problem ({type(e).__name__}) for {path[:60]}, '
                      f'retrying in {delay}s')
                time.sleep(delay)

    def json(self, path):
        return self._retrying(path, json.load)

    def download_gz(self, path, destination):
        """Stream a response to destination as gzip; returns False on 404."""
        temporary = destination + '.part'

        def read(response):
            with gzip.open(temporary, 'wb') as out:
                shutil.copyfileobj(response, out, 1 << 20)
            return True

        if not self._retrying(path, read):
            return False
        os.replace(temporary, destination)
        return True


def quote(path):
    return urllib.parse.quote(path, safe="/:?&=,[]{}.*'")
