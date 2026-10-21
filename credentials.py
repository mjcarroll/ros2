"""Store the Jenkins user id and API token encrypted for the current Windows user (DPAPI).

    python credentials.py set ci       # prompts for the user id and, hidden, the token
    python credentials.py check ci     # shows where it is stored and the user id, never the token
    python credentials.py clear ci

The file lives in %LOCALAPPDATA%\\ros2_ci_observability and can only be decrypted by the same Windows
account on the same machine. Environment variables (JENKINS_<SERVER>_USER / _TOKEN) still take precedence.
"""

import ctypes
import ctypes.wintypes as wintypes
import getpass
import os
import sys

DIRECTORY = os.environ.get('ROS2_CI_CREDENTIALS_DIR') or os.path.join(
    os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'ros2_ci_observability')


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _crypt(function, data):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    incoming = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    outgoing = _Blob()
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ok = function(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def protect(data):
    return _crypt(ctypes.windll.crypt32.CryptProtectData, data)


def unprotect(data):
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData, data)


def _path(server):
    return os.path.join(DIRECTORY, f'{server}.cred')


def save(server, user, token):
    os.makedirs(DIRECTORY, exist_ok=True)
    with open(_path(server), 'wb') as f:
        f.write(protect(f'{user}\n{token}'.encode()))


def load(server):
    """Return (user, token) or None if nothing is stored or it cannot be decrypted here."""
    try:
        with open(_path(server), 'rb') as f:
            user, token = unprotect(f.read()).decode().split('\n', 1)
    except (OSError, ValueError, AttributeError):
        return None
    return user, token


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('set', 'check', 'clear'):
        raise SystemExit(__doc__)
    action, server = sys.argv[1:]
    if action == 'set':
        user = input(f'Jenkins user id for {server}: ').strip()
        token = getpass.getpass('API token (hidden): ').strip()
        if not (user and token):
            raise SystemExit('both are required; nothing stored')
        save(server, user, token)
        print(f'stored encrypted at {_path(server)}')
    elif action == 'check':
        stored = load(server)
        print(f'{_path(server)}: ' + (f'stored for user {stored[0]!r}' if stored else 'nothing usable stored'))
    else:
        try:
            os.remove(_path(server))
            print('removed')
        except FileNotFoundError:
            print('nothing to remove')


if __name__ == '__main__':
    main()
