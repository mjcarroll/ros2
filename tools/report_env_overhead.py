#!/usr/bin/env python3
"""Report how much of a colcon build went into deriving package environments.

colcon emits a ``JobStarted`` event when a package's job acquires a worker slot
and a ``Command`` event when it finally launches its first subprocess.  The gap
between the two is the command-environment derivation: the shell extensions
spend it dot-sourcing every dependency's package script, the .dsv extension
spends it walking descriptors in-process.

Run this after a build to get a number that can be compared across the two.

Usage:
    report_env_overhead.py [LOG_BASE_OR_EVENTS_LOG]

Defaults to ./log, picking the most recent build_* directory in it.  Windows
runners have no ``log/latest_build`` symlink, hence the search.
"""

import os
import re
import sys
from pathlib import Path

EVENT_RE = re.compile(r'^\[(\d+\.\d+)\] \((.*?)\) (\w+):')


def find_events_log(arg):
    path = Path(arg)
    if path.is_file():
        return path
    if path.name == 'events.log':
        return path
    candidates = sorted(
        (p for p in path.glob('build_*') if (p / 'events.log').is_file()),
        key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit('no build_*/events.log found under %s' % path)
    return candidates[-1] / 'events.log'


def parse(events_log):
    started, first_cmd, ended = {}, {}, {}
    wall = 0.0
    with events_log.open(encoding='utf-8', errors='replace') as handle:
        for line in handle:
            match = EVENT_RE.match(line)
            if not match:
                continue
            stamp, pkg, kind = float(match.group(1)), match.group(2), match.group(3)
            wall = max(wall, stamp)
            if kind == 'JobStarted':
                started[pkg] = stamp
            elif kind == 'Command':
                first_cmd.setdefault(pkg, stamp)
            elif kind == 'JobEnded':
                ended[pkg] = stamp
    rows = [
        (first_cmd[pkg] - start, ended.get(pkg, first_cmd[pkg]) - start, pkg)
        for pkg, start in started.items() if pkg in first_cmd
    ]
    return wall, sorted(rows, reverse=True)


def main():
    events_log = find_events_log(sys.argv[1] if len(sys.argv) > 1 else 'log')
    wall, rows = parse(events_log)
    if not rows:
        raise SystemExit('no completed jobs in %s' % events_log)

    setup = sum(r[0] for r in rows)
    injob = sum(r[1] for r in rows)
    share = 100 * setup / injob if injob else 0

    lines = [
        'Environment derivation: %s' % events_log,
        '',
        '  packages                 : %d' % len(rows),
        '  build wall clock         : %8.1f s' % wall,
        '  time inside jobs         : %8.1f s' % injob,
        '  spent deriving env       : %8.1f s  (%.0f%% of in-job time)'
        % (setup, share),
        '  mean per package         : %8.2f s' % (setup / len(rows)),
        '  median per package       : %8.2f s' % sorted(r[0] for r in rows)[len(rows) // 2],
        '',
        '  worst 10 packages:',
    ]
    for pre, total, pkg in rows[:10]:
        lines.append('    %-44s %7.2f s of %7.2f s' % (pkg, pre, total))

    report = '\n'.join(lines)
    print(report)

    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write('### Environment derivation\n\n```\n%s\n```\n' % report)


if __name__ == '__main__':
    main()
