"""Daily refresh: start the stack, fetch new nightly builds from ci.ros2.org, export them.

    python refresh.py [--last 3]

Meant for Task Scheduler. Credentials come from the encrypted store (see credentials.py). Output goes to
logs/refresh-YYYY-MM-DD.log. Exit code is 0 only if every step succeeded.
"""

import argparse
import datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, 'cache', '.refresh.lock')


def run(log, name, command, **kwargs):
    log.write(f'\n== {name}: {" ".join(command)}\n')
    log.flush()
    result = subprocess.run(command, cwd=HERE, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    log.write(f'== {name} exited {result.returncode}\n')
    log.flush()
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--last', type=int, default=3, help='builds to look back per job')
    parser.add_argument('--match', default='nightly_')
    args = parser.parse_args()

    os.makedirs(os.path.join(HERE, 'logs'), exist_ok=True)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    try:
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        raise SystemExit(f'another refresh is running (or crashed); delete {LOCK} if stale')
    log_path = os.path.join(HERE, 'logs', f'refresh-{datetime.date.today()}.log')
    try:
        with open(log_path, 'a', encoding='utf-8') as log:
            log.write(f'\n##### refresh started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n')
            python = sys.executable
            stack = run(log, 'stack', ['docker', 'compose', 'up', '-d'])
            fetched = stack and run(log, 'fetch', [python, '-u', 'fetch.py', '--server', 'ci',
                                                   '--match', args.match, '--last', str(args.last)])
            # export even if the fetch had problems: it only touches complete builds
            exported = stack and run(log, 'export', [python, '-u', 'export.py', '--every-job'])
            ok = bool(stack and fetched and exported)
            log.write(f'##### refresh finished {datetime.datetime.now():%Y-%m-%d %H:%M:%S} '
                      f'{"ok" if ok else "WITH PROBLEMS"}\n')
        sys.exit(0 if ok else 1)
    finally:
        os.remove(LOCK)


if __name__ == '__main__':
    main()
