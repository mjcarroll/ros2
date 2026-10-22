"""Download builds from a Jenkins server into ./cache for offline parsing.

    python fetch.py --server ci --job nightly_win_rel --builds 3959 3946
    python fetch.py --server ci --job nightly_win_rel --last 10

Per build it stores build.json, testReport.json.gz and a timestamped console.log.gz under
cache/<server>/<job>/<number>/. Finished builds already in the cache are skipped. Credentials come
from the environment (see check_auth.py); nothing is written that contains them.
"""

import argparse
import json
import os

from jenkins import Jenkins, quote

BUILD_TREE = (
    'number,result,building,timestamp,duration,builtOn,fullDisplayName,'
    'actions[queuingDurationMillis,waitingDurationMillis,buildableDurationMillis,'
    'executingTimeMillis,totalDurationMillis,failCount,skipCount,totalCount,'
    'parameters[name,value],causes[upstreamProject,upstreamBuild,userId,userName]]')
TEST_TREE = (
    'failCount,passCount,skipCount,'
    'suites[name,duration,cases[className,name,status,duration,age,errorDetails]]')
# Timestamper: one line per console line, UTC, millisecond resolution.
CONSOLE = '/timestamps/?time=yyyy-MM-dd%20HH:mm:ss.SSS&timeZone=UTC&appendLog'


def fetch_build(jenkins, server, job, number, root):
    directory = os.path.join(root, server, job, str(number))
    marker = os.path.join(directory, 'build.json')
    if os.path.exists(marker):
        return 'cached'
    base = f'/job/{job}/{number}'
    info = jenkins.json(quote(f'{base}/api/json?tree={BUILD_TREE}'))
    if info is None:
        return 'missing'
    if info.get('building'):
        return 'still running, skipped'
    os.makedirs(directory, exist_ok=True)
    jenkins.download_gz(quote(f'{base}/testReport/api/json?tree={TEST_TREE}'),
                        os.path.join(directory, 'testReport.json.gz'))
    jenkins.download_gz(f'{base}{CONSOLE}', os.path.join(directory, 'console.log.gz'))
    with open(marker, 'w') as f:  # written last: its presence means the build is complete
        json.dump(info, f)
    return info.get('result', '?')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--server', choices=['ci', 'build'], required=True)
    parser.add_argument('--job', nargs='*', default=[], help='one or more job names')
    parser.add_argument('--match', help='also fetch every job whose name starts with this, e.g. nightly_')
    parser.add_argument('--builds', type=int, nargs='*', default=[])
    parser.add_argument('--last', type=int, default=0, help='also fetch the last N builds of each job')
    parser.add_argument('--cache', default=os.path.join(os.path.dirname(__file__), 'cache'))
    args = parser.parse_args()

    jenkins = Jenkins(args.server)
    jobs = list(args.job)
    if args.match:
        listing = jenkins.json('/api/json?tree=jobs[name]') or {}
        jobs += sorted(j['name'] for j in listing.get('jobs', [])
                       if j['name'].startswith(args.match) and j['name'] not in jobs)
    if not jobs:
        raise SystemExit('give --job and/or --match')
    failures = []
    for job in jobs:
        print(f'== {job}')
        numbers = set(args.builds)
        if args.last:
            listing = jenkins.json(quote(f'/job/{job}/api/json?tree=builds[number]{{0,{args.last}}}'))
            numbers.update(b['number'] for b in (listing or {}).get('builds', []))
        for number in sorted(numbers, reverse=True):
            print(f'{job} #{number}: ', end='', flush=True)
            try:
                print(fetch_build(jenkins, args.server, job, number, args.cache))
            except Exception as e:  # noqa: B902
                print(f'FAILED ({type(e).__name__}: {e}); rerun to retry')
                failures.append((job, number))
    print(f'done, {len(failures)} build(s) failed' + (': ' + ', '.join(f'{j} #{n}' for j, n in failures)
                                                     if failures else ''))


if __name__ == '__main__':
    main()
