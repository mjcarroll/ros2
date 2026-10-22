"""Daily refresh: start the stack, fetch new nightly builds from ci.ros2.org, export them, snapshot
build.ros2.org's queue/agents and infra job health, and track the on-demand ci_*/test_ci_* jobs
(who requested them, their scope, and their results) that ci_launcher kicks off per PR.

    python refresh.py [--last 3]

Meant for Task Scheduler. Credentials come from the encrypted store (see credentials.py). Output goes to
logs/refresh-YYYY-MM-DD.log. Exit code is 0 only if every step succeeded.

check_agents / ci__infra_* are the conventional names for build.ros2.org's own health-check jobs;
--build-infra-match lets you correct the prefix once you've confirmed it against the live server.
"""

import argparse
import datetime
import os
import subprocess
import sys

from jenkins import Jenkins

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, 'cache', '.refresh.lock')


def run(log, name, command, **kwargs):
    log.write(f'\n== {name}: {" ".join(command)}\n')
    log.flush()
    result = subprocess.run(command, cwd=HERE, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    log.write(f'== {name} exited {result.returncode}\n')
    log.flush()
    return result.returncode == 0


def matched_jobs(server, prefix):
    listing = Jenkins(server).json('/api/json?tree=jobs[name]') or {}
    return sorted(j['name'] for j in listing.get('jobs', []) if j['name'].startswith(prefix))


def refresh_job(log, python, server, job, last):
    # Export right after that job's fetch, not after every job has been fetched, so a dashboard
    # fills in job by job instead of sitting empty until the very end.
    fetched = run(log, f'fetch {server}/{job}', [python, '-u', 'fetch.py', '--server', server,
                                                 '--job', job, '--last', str(last)])
    # export even if the fetch had problems: it only touches complete builds
    exported = run(log, f'export {server}/{job}', [python, '-u', 'export.py', '--server', server,
                                                    '--job', job])
    return fetched, exported


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--last', type=int, default=3, help='builds to look back per job')
    parser.add_argument('--match', default='nightly_')
    parser.add_argument('--ondemand-match', nargs='*', default=['ci_', 'test_ci_'],
                        help='ci.ros2.org job name prefixes for on-demand (ci_launcher-triggered) jobs')
    parser.add_argument('--build-infra-match', default='ci__infra_',
                        help='build.ros2.org job name prefix for infra/check jobs')
    parser.add_argument('--build-infra-job', nargs='*', default=['check_agents'],
                        help='build.ros2.org exact infra/check job names')
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
            ok = stack
            if stack:
                ci_jobs = matched_jobs('ci', args.match)
                ondemand_jobs = sorted({j for prefix in args.ondemand_match
                                        for j in matched_jobs('ci', prefix)})
                build_jobs = sorted(set(matched_jobs('build', args.build_infra_match))
                                    | set(args.build_infra_job))
                for job in ci_jobs + ondemand_jobs:
                    _, exported = refresh_job(log, python, 'ci', job, args.last)
                    ok = ok and exported
                for job in build_jobs:
                    _, exported = refresh_job(log, python, 'build', job, args.last)
                    ok = ok and exported
                ok = ok and run(log, 'buildfarm ci', [python, '-u', 'buildfarm.py', '--server', 'ci'])
                ok = ok and run(log, 'buildfarm build',
                                [python, '-u', 'buildfarm.py', '--server', 'build'])
            ok = bool(ok)
            log.write(f'##### refresh finished {datetime.datetime.now():%Y-%m-%d %H:%M:%S} '
                      f'{"ok" if ok else "WITH PROBLEMS"}\n')
        sys.exit(0 if ok else 1)
    finally:
        os.remove(LOCK)


if __name__ == '__main__':
    main()
