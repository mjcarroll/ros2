"""Turn cached Jenkins builds into traces, logs and metrics and send them to the local OTLP endpoint.

    python export.py --server ci --job nightly_win_rel --builds 3959 --dry-run
    python export.py --server ci --job nightly_win_rel --all

Works only on ./cache (see fetch.py). Trace and span ids are derived from the build, so re-running
replaces rather than duplicates. Timing notes:
  * section, package build/test spans come from live console lines and are accurate;
  * per-test spans are estimates: colcon flushes a package's output in one block after it finishes,
    so tests are laid out back to back from the package start (marked ci.timing=estimated).
"""

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import github

OTLP = 'http://localhost:4318'
GRAFANA = 'http://localhost:3300/api/datasources/proxy/uid'
BATCH = 1000
# The Collector ACKs a /v1/logs POST as soon as it's queued, before the real write to Loki
# completes, so an unpaced client can blast requests faster than the single-consumer queue
# drains. A small per-batch delay keeps submission rate under what Loki can sustain, instead of
# growing the queue (which risks unbounded memory on a build with hundreds of thousands of lines).
LOG_POST_DELAY = 0.05
# A hung process spewing output (seen: one ABORTED Windows build with 3M+ console lines) can
# overwhelm the pipeline badly enough to drop other builds' data too. Cap it: keep the start and
# end, which is what's actually useful for diagnosing a failure, and drop the noisy middle.
MAX_LOG_LINES = 50_000

LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})  ?(.*)$')
SECTION = re.compile(r'^"?# (BEGIN|END) SECTION(?:: (.*?))?"?\s*$')
SUBSECTION = re.compile(r'^# (BEGIN|END) SUBSECTION(?:: (.*?))?\s*$')
STARTING = re.compile(r'^Starting >>> (\S+)')
FINISHED = re.compile(r'^(Finished|Failed|Aborted)\s+<<< (\S+)(?: \[.*?exited with code (-?\d+)\])?')
OUTPUT = re.compile(r'^--- output: (\S+)')
CTEST = re.compile(r'^\s*\d+/\d+ Test\s+#(\d+):\s+(\S+)\s+\.+\**\s*(.+?)\s+([\d.]+) sec')
REPO = re.compile(r'^  (\S+):\s*$')
VERSION = re.compile(r'^    version: (\S+)')
URLLINE = re.compile(r'^    url: (\S+)')
SLOW_TEST_SECONDS = 5.0
MIN_GROUP_TESTS = 10   # test names seen in at least this many packages (cpplint, xmllint, ...)
ERRORISH = re.compile(
    r'\[ERROR\]|\berror:|\*\*\*(Failed|Timeout|Exception)|Windows fatal exception|access violation|'
    r'Build timed out|Traceback \(most recent|\bFAILED\b')
PKG_MIN_SECONDS = 60.0   # shorter finished packages are folded into counts on their phase span
TEST_MIN_SECONDS = 30.0  # same for passing tests; failed tests always get a span
PACKAGES_ABOVE = re.compile(r'--packages-above(?:-and-dependencies)?\s+(.+?)(?:\s+--\S|\s*$)')
# ci_*/test_ci_* are manually triggered per build: whoever kicks one off picks a branch/sha/tag or a
# fully custom ros2.repos file. Consecutive build numbers there are unrelated people's runs, not
# incremental progress on one branch, so "repos changed since the previous build" is meaningless.
ONDEMAND_JOB = re.compile(r'^(ci_|test_ci_)')


def trigger_user(info):
    """The human who clicked "Build" (UserIdCause), if any, directly on this build."""
    return next((c['userId'] for a in info.get('actions', []) for c in a.get('causes', [])
                 if c.get('userId')), None)


def signature(text):
    """Normalise an error line so the same message with different numbers or paths groups together."""
    s = re.sub(r'^\s*\d+:\s*', '', text)
    s = re.sub(r'\[\d{9,}\.\d+\]', '[T]', s)
    s = re.sub(r'\b[A-Za-z]:\\[^\s:]*|(?:/[\w.+@-]+){2,}', '<path>', s)
    s = re.sub(r'0x[0-9a-fA-F]+|\b[0-9a-f]{8,}\b', '<hex>', s)
    s = re.sub(r'\d+', 'N', s)
    return s.strip()[:110]


def load_prev_repos(root, number):
    """Repos (commit, url) of the nearest earlier build in the cache that has them, or None."""
    earlier = sorted((int(n) for n in os.listdir(root) if n.isdigit() and int(n) < number), reverse=True)
    for n in earlier:
        path = os.path.join(root, str(n), 'repos.json')
        if os.path.exists(path):
            with open(path) as f:
                return n, json.load(f)
    return None


def save_repos(root, number, repos):
    if repos:
        with open(os.path.join(root, str(number), 'repos.json'), 'w') as f:
            json.dump(repos, f)


def repo_diff_summary(repos, prev_repos):
    """Human-readable, GitHub-enriched summary of what changed between two repos.json snapshots."""
    parts = []
    for r, now in repos.items():
        before = prev_repos.get(r)
        if before is None or before['commit'] == now['commit']:
            continue
        url = now['url'].removesuffix('.git')
        commits = github.compare(url, before['commit'], now['commit'])
        if commits:
            authors = ', '.join(sorted({c['author'] for c in commits}))
            parts.append(f'{r} ({len(commits)} commit(s) by {authors}): {commits[-1]["message"]}' +
                        (f' [+{len(commits) - 1} more]' if len(commits) > 1 else ''))
        else:
            parts.append(f'{r} {before["commit"][:8]}..{now["commit"][:8]}')
    return parts


def regression_culprits(root, build_number):
    """repo_diff_summary() for a specific past build, using its saved repos.json, or None if we
    don't have that build's (or its predecessor's) repo snapshot on disk."""
    if root is None:
        return None
    path = os.path.join(root, str(build_number), 'repos.json')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        repos = json.load(f)
    prev = load_prev_repos(root, build_number)
    if not prev:
        return None
    return repo_diff_summary(repos, prev[1]) or None


def epoch_ns(text):
    dt = datetime.strptime(text, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) * 1_000_000


def hex_id(text, nbytes):
    return hashlib.sha256(text.encode()).hexdigest()[:nbytes * 2]


def attr(key, value):
    if isinstance(value, bool):
        v = {'boolValue': value}
    elif isinstance(value, int):
        v = {'intValue': str(value)}
    elif isinstance(value, float):
        v = {'doubleValue': value}
    else:
        v = {'stringValue': str(value)}
    return {'key': key, 'value': v}


def attrs(mapping):
    return [attr(k, v) for k, v in mapping.items() if v is not None and v != '']


class Builder:

    def __init__(self, server, job, number, info, console, report, prev=None, cache=None):
        self.server, self.job, self.number = server, job, number
        self.prev, self.cache = prev, cache
        self.root = os.path.join(cache, server, job) if cache else None
        self.repos = {}
        self.info, self.report = info, report
        self.trace_id = hex_id(f'{server}/{job}/{number}', 16)
        self.start = int(info['timestamp']) * 1_000_000
        self.end = self.start + int(info['duration']) * 1_000_000
        self.spans, self.logs, self.metrics = [], [], []
        self.by_id = {}
        self.root_id = self.span_id('build')
        self._parse(console)

    def span_id(self, *parts):
        return hex_id(self.trace_id + '/' + '/'.join(parts), 8)

    def span(self, name, start, end, parent, key, attributes=None, error=False, links=None):
        span = {
            'traceId': self.trace_id, 'spanId': self.span_id(key), 'name': name, 'kind': 1,
            'startTimeUnixNano': str(start), 'endTimeUnixNano': str(max(end, start)),
            'attributes': attrs(attributes or {}),
            'status': {'code': 2} if error else {},
        }
        if parent:
            span['parentSpanId'] = parent
        if links:
            span['links'] = links
        self.spans.append(span)
        self.by_id[span['spanId']] = span
        return span['spanId']

    def annotate(self, span_id, mapping):
        span = self.by_id.get(span_id)
        if span is not None:
            span['attributes'] += attrs(mapping)

    def _parse(self, console):
        common = {'ci.server': self.server, 'ci.job': self.job, 'ci.build': self.number}
        sections, subsections = [], []   # (name, start, end, id)
        open_section = open_sub = None
        phase_at = []                    # (start, end, name) for build/test subsections
        running = {}                     # (package, phase) -> start, line index
        packages = {}                    # (package, phase) -> (span id, start, end)
        output_pkg = None
        tests = []                       # (package, number, name, status, seconds)
        hashes, urls, in_hashes, repo = {}, {}, False, None
        signatures = collections.Counter()
        last_ts = self.start

        def phase_of(ts):
            if open_sub and open_sub[0] in ('build', 'test'):
                return open_sub[0]
            return 'other'

        def current_section():
            if open_sub:
                return open_sub[0]
            if open_section:
                return open_section[0]
            return None

        for raw in console:
            m = LINE.match(raw.rstrip('\r\n'))
            if m:
                last_ts, text = epoch_ns(m.group(1)), m.group(2)
            else:
                text = raw.rstrip('\r\n')
            ts = last_ts

            m = SECTION.match(text)
            if m:
                if m.group(1) == 'BEGIN':
                    open_section = [m.group(2), ts]
                elif open_section:
                    sections.append((open_section[0], open_section[1], ts))
                    open_section = None
                continue
            m = SUBSECTION.match(text)
            if m:
                if m.group(1) == 'BEGIN':
                    open_sub = [m.group(2), ts]
                    in_hashes = m.group(2) == 'vcs export --exact'
                elif open_sub:
                    subsections.append((open_sub[0], open_sub[1], ts))
                    if open_sub[0] in ('build', 'test'):
                        phase_at.append((open_sub[1], ts, open_sub[0]))
                    open_sub, in_hashes = None, False
                continue
            if in_hashes:
                m = REPO.match(text)
                if m:
                    repo = m.group(1)
                m = VERSION.match(text)
                if m and repo:
                    hashes[repo] = m.group(1)
                m = URLLINE.match(text)
                if m and repo:
                    urls[repo] = m.group(1)
                continue
            m = STARTING.match(text)
            if m:
                running[(m.group(1), phase_of(ts))] = ts
                self.logs.append((ts, 'INFO', text[:2000],
                                  {'_ctx': ('pkg', m.group(1), phase_of(ts))}))
                continue
            m = FINISHED.match(text)
            if m:
                package, state, code = m.group(2), m.group(1), m.group(3)
                phase = phase_of(ts)
                start = running.pop((package, phase), None)
                if start is not None:
                    packages[(package, phase)] = (start, ts, state, code)
                self.logs.append((ts, 'INFO' if state == 'Finished' else 'ERROR', text[:2000],
                                  {'_ctx': ('pkg', package, phase)}))
                output_pkg = None
                continue
            m = OUTPUT.match(text)
            if m:
                output_pkg = m.group(1)
                continue
            m = CTEST.match(text)
            if m and output_pkg:
                tests.append((output_pkg, int(m.group(1)), m.group(2), m.group(3), float(m.group(4))))
                self.logs.append((ts, 'INFO', text[:2000],
                                  {'_ctx': ('pkg', output_pkg, phase_of(ts))}))
                continue
            error = bool(ERRORISH.search(text))
            if error:
                signatures[signature(text)] += 1
            if output_pkg:
                ctx = ('pkg', output_pkg, phase_of(ts))
            else:
                section = current_section()
                ctx = ('phase', section) if section else None
            self.logs.append((ts, 'ERROR' if error else 'INFO', text[:2000], {'_ctx': ctx}))
        if open_section:
            sections.append((open_section[0], open_section[1], self.end))
        if open_sub:
            subsections.append((open_sub[0], open_sub[1], self.end))
            if open_sub[0] in ('build', 'test'):
                phase_at.append((open_sub[1], self.end, open_sub[0]))

        result = self.info.get('result') or 'UNKNOWN'
        params = {p['name']: p['value'] for a in self.info.get('actions', [])
                  for p in a.get('parameters', []) if p['name'] in (
                      'CI_BRANCH_TO_TEST', 'CI_CMAKE_BUILD_TYPE', 'CI_ROS_DISTRO', 'CI_USE_CONNEXTDDS',
                      'CI_UBUNTU_DISTRO', 'CI_BUILD_ARGS')}
        queued = next((a.get('queuingDurationMillis') for a in self.info.get('actions', [])
                       if a.get('queuingDurationMillis') is not None), None)
        tests_summary = next((a for a in self.info.get('actions', []) if a.get('totalCount')), {})
        upstream = next((c for a in self.info.get('actions', []) for c in a.get('causes', [])
                         if c.get('upstreamProject')), None)
        # Who clicked "Build": set directly, or (for jobs launched by ci_launcher) one hop up.
        requested_by = trigger_user(self.info)
        if requested_by is None and upstream and self.cache:
            upstream_path = os.path.join(self.cache, self.server, upstream['upstreamProject'],
                                         str(upstream['upstreamBuild']), 'build.json')
            if os.path.exists(upstream_path):
                with open(upstream_path) as f:
                    requested_by = trigger_user(json.load(f))
        target_package = None
        m = PACKAGES_ABOVE.search(params.get('CI_BUILD_ARGS', ''))
        if m:
            target_package = m.group(1).strip()[:200]
        links = None
        if upstream:
            upstream_trace = hex_id(f'{self.server}/{upstream["upstreamProject"]}/'
                                    f'{upstream["upstreamBuild"]}', 16)
            links = [{'traceId': upstream_trace,
                     'spanId': hex_id(upstream_trace + '/build', 8)}]
        self.span(f'{self.job} #{self.number}', self.start, self.end, None, 'build', {
            **common, 'ci.result': result, 'ci.agent': self.info.get('builtOn'),
            'ci.queue_ms': queued, 'ci.tests.failed': tests_summary.get('failCount'),
            'ci.tests.total': tests_summary.get('totalCount'),
            'ci.upstream_job': upstream and upstream['upstreamProject'],
            'ci.upstream_build': upstream and upstream['upstreamBuild'],
            'ci.requested_by': requested_by, 'ci.target_package': target_package,
            **{f'ci.param.{k}': v for k, v in params.items()}},
            error=result in ('FAILURE', 'ABORTED'), links=links)
        if upstream:
            self.metrics.append(('ci_triggered_by', self.end, 1.0, {
                'ci_build': self.number, 'ci_trace_id': self.trace_id, 'ci_result': result,
                'ci_upstream_job': upstream['upstreamProject'],
                'ci_upstream_build': upstream['upstreamBuild']}))
        if queued:
            self.span('queue', self.start - queued * 1_000_000, self.start, self.root_id, 'queue',
                      common)

        top = {}
        for name, s, e in sections:
            top[name] = self.span(name, s, e, self.root_id, f'section/{name}', common)
        for name, start_ns, end_ns in sections:
            self.metrics.append(('ci_section_duration_seconds', end_ns, (end_ns - start_ns) / 1e9,
                                 {'ci_section': name, 'ci_kind': 'section'}))
        for name, start_ns, end_ns in subsections:
            self.metrics.append(('ci_section_duration_seconds', end_ns, (end_ns - start_ns) / 1e9,
                                 {'ci_section': name, 'ci_kind': 'subsection'}))
        run_section = top.get('Run DockerFile', self.root_id)
        sub_ids = {}
        for name, s, e in subsections:
            sub_ids[name] = self.span(name, s, e, run_section, f'sub/{name}', common)

        # Packages still open when the build ended hung or were cut off by the timeout.
        for (package, phase), start in running.items():
            packages[(package, phase)] = (start, self.end, 'Unfinished', None)
        package_ids = {}
        folded = {}
        for (package, phase), (start, end, state, code) in packages.items():
            parent = sub_ids.get(phase, self.root_id)
            problem = {'ci_build': self.number, 'ci_trace_id': self.trace_id} \
                if state != 'Finished' else {}
            self.metrics.append(('ci_package_duration_seconds', end, (end - start) / 1e9, {
                'ci_package': package, 'ci_phase': phase, 'ci_state': state, **problem}))
            if state == 'Unfinished':
                self.logs.append((end, 'ERROR', f'{phase} {package} still running when the build ended',
                                  {'ci.package': package, 'ci.phase': phase,
                                   '_ctx': ('pkg', package, phase)}))
            if state == 'Finished' and (end - start) / 1e9 < PKG_MIN_SECONDS:
                count = folded.setdefault(phase, [0, 0.0])
                count[0] += 1
                count[1] += (end - start) / 1e9
                continue
            package_ids[(package, phase)] = (self.span(
                f'{phase} {package}', start, end, parent, f'pkg/{phase}/{package}', {
                    **common, 'ci.package': package, 'ci.phase': phase, 'ci.state': state,
                    'ci.exit_code': int(code) if code else None},
                error=state != 'Finished'), start, end)
        for phase, (count, seconds) in folded.items():
            self.annotate(sub_ids.get(phase), {
                'ci.packages.folded': count, 'ci.packages.folded.seconds': round(seconds, 1),
                'ci.packages.span_min_seconds': PKG_MIN_SECONDS})

        cursor, seen, agg, test_spans = {}, {}, {}, {}
        name_total, name_count = collections.Counter(), collections.Counter()
        for package, number, name, status, seconds in tests:
            name_total[name] += seconds
            name_count[name] += 1
            if seconds >= SLOW_TEST_SECONDS:
                self.metrics.append(('ci_test_duration_seconds', self.end, seconds,
                                     {'ci_package': package, 'ci_test': name[:150]}))
            seen[package] = seen.get(package, 0) + 1
            entry = package_ids.get((package, 'test'))
            if not entry:
                continue
            span_id, start, end = entry
            total = agg.setdefault(package, {'ci.tests.count': 0, 'ci.tests.failed': 0,
                                             'ci.tests.seconds': 0.0})
            total['ci.tests.count'] += 1
            total['ci.tests.seconds'] = round(total['ci.tests.seconds'] + seconds, 2)
            bad = bool(re.search(r'Fail|Timeout|Exception|Not Run|Segfault', status))
            total['ci.tests.failed'] += 1 if bad else 0
            begin = cursor.get(package, start)
            finish = min(begin + int(seconds * 1e9), end)
            cursor[package] = finish
            if not bad and seconds < TEST_MIN_SECONDS:
                continue
            test_span = self.span(f'test {name}', begin, finish, span_id, f'test/{package}/{seen[package]}', {
                **common, 'ci.package': package, 'ci.test': name, 'ci.test.status': status,
                'ci.test.seconds': seconds, 'ci.timing': 'estimated'}, error=bad)
            test_spans.setdefault(name, test_span)
            # So every real span has at least one log line, even a slow-but-passing test.
            self.logs.append((finish, 'ERROR' if bad else 'INFO',
                              f'test {name}: {status} ({seconds:.1f}s)',
                              {'ci.package': package, 'ci.test': name, '_ctx': ('test', name)}))
        for package, mapping in agg.items():
            self.annotate(package_ids[(package, 'test')][0], mapping)
        for name, total in name_total.most_common(60):
            if name_count[name] >= MIN_GROUP_TESTS:
                self.metrics.append(('ci_test_total_seconds', self.end, total, {'ci_test': name[:150]}))
                self.metrics.append(('ci_test_count', self.end, float(name_count[name]),
                                     {'ci_test': name[:150]}))

        if self.report:
            for key, metric in (('passCount', 'ci_tests_passed'), ('skipCount', 'ci_tests_skipped')):
                if self.report.get(key) is not None:
                    self.metrics.append((metric, self.end, float(self.report[key]),
                                         {'ci_build': self.number}))
            for suite in self.report.get('suites', []):
                for case in suite.get('cases', []):
                    if case.get('status') in ('FAILED', 'REGRESSION'):
                        age = int(case.get('age') or 1)
                        first_failed = int(self.number) - age + 1
                        self.metrics.append(('ci_test_failure', self.end, float(age), {
                            'ci_build': self.number, 'ci_first_failed_build': first_failed,
                            'ci_test_class': (case.get('className') or '')[:200],
                            'ci_test': (case.get('name') or '')[:200],
                            'ci_status': case.get('status')}))
                        detail = (case.get('errorDetails') or '')[:1000]
                        culprits = (None if ONDEMAND_JOB.match(self.job) else
                                   regression_culprits(self.root, first_failed))
                        if culprits:
                            detail += (f'\n\nLikely culprit (repos changed in build #{first_failed}, '
                                      f'where this first failed):\n' + '\n'.join(culprits))
                        self.logs.append((self.end, 'ERROR', detail, {
                            'ci.test.class': case.get('className'), 'ci.test.name': case.get('name'),
                            'ci.test.age': case.get('age'), 'ci.test.status': case.get('status'),
                            '_ctx': ('test', case.get('name'))}))
        def span_for(ctx):
            if not ctx:
                return self.root_id
            if ctx[0] == 'pkg':
                entry = package_ids.get((ctx[1], ctx[2]))
                return entry[0] if entry else sub_ids.get(ctx[2], self.root_id)
            if ctx[0] == 'test':
                return test_spans.get(ctx[1], self.root_id)
            return sub_ids.get(ctx[1]) or top.get(ctx[1], self.root_id)

        for record in self.logs:
            extra = record[3]
            ctx = extra.pop('_ctx', None)
            extra['_span'] = span_for(ctx)
            if ctx and ctx[0] == 'pkg':
                extra['ci.package'], extra['ci.phase'] = ctx[1], ctx[2]
        self.repos = {r.replace('\\', '/'): {'commit': c, 'url': urls.get(r, '')}
                      for r, c in hashes.items()}
        if self.prev and not ONDEMAND_JOB.match(self.job):
            prev_number, prev_repos = self.prev
            for r, now in self.repos.items():
                before = prev_repos.get(r)
                if before is None or before['commit'] == now['commit']:
                    continue
                self.metrics.append(('ci_repo_changed', self.end, 1.0, {
                    'ci_build': self.number, 'ci_prev_build': prev_number, 'ci_repo': r,
                    'ci_old': before['commit'][:12], 'ci_new': now['commit'][:12],
                    'ci_url': now['url'].removesuffix('.git')}))
            summary = repo_diff_summary(self.repos, prev_repos)
            if summary:
                self.logs.append((self.end, 'INFO', 'Repos changed since the previous build:\n' +
                                  '\n'.join(summary), {'ci.kind': 'repos_changed'}))
        if hashes:
            self.logs.append((self.start, 'INFO', json.dumps(hashes, sort_keys=True),
                              {'ci.kind': 'repository_hashes', 'ci.repos': len(hashes)}))

        end = self.end
        for sig, count in signatures.most_common(40):
            self.metrics.append(('ci_error_signature_count', end, float(count),
                                 {'ci_signature': sig, 'ci_build': self.number}))
        self.metrics += [
            ('ci_build_duration_seconds', end, (self.end - self.start) / 1e9,
             {'ci_result': result, 'ci_build': self.number, 'ci_requested_by': requested_by}),
            ('ci_build_result', end, 1.0,
             {'ci_result': result, 'ci_build': self.number, 'ci_trace_id': self.trace_id,
              'ci_requested_by': requested_by, 'ci_target_package': target_package}),
        ]
        if queued is not None:
            self.metrics.append(('ci_build_queue_seconds', end, queued / 1000, {}))
        if tests_summary.get('totalCount'):
            self.metrics.append(('ci_tests_failed', end, float(tests_summary.get('failCount', 0)),
                                 {'ci_build': self.number}))
        self.common = {'ci.server': self.server, 'ci.job': self.job}
        self.agent = self.info.get('builtOn')

    def resource(self, per_build=False):
        # Loki rejects entries older than a stream's newest, so each build gets its own stream.
        extra = {'service.namespace': self.job, 'service.instance.id': str(self.number)}             if per_build else {}
        return {'attributes': attrs({
            'service.name': f'jenkins-{self.server}', 'ci.server': self.server, **extra})}

    def otlp_traces(self, spans):
        return {'resourceSpans': [{'resource': self.resource(), 'scopeSpans': [{'spans': spans}]}]}

    def otlp_logs(self, records):
        return {'resourceLogs': [{'resource': self.resource(per_build=True), 'scopeLogs': [{'logRecords': [{
            'timeUnixNano': str(ts), 'severityText': sev, 'body': {'stringValue': body},
            'traceId': self.trace_id, 'spanId': extra.get('_span') or self.root_id,
            'attributes': attrs({**self.common, 'ci.build': self.number,
                                 **{k: v for k, v in extra.items() if not k.startswith('_')}}),
        } for ts, sev, body, extra in records]}]}]}

    def otlp_metrics(self):
        by_name = {}
        for name, ts, value, labels in self.metrics:
            by_name.setdefault(name, []).append({
                'timeUnixNano': str(ts), 'asDouble': value,
                'attributes': attrs({'ci_server': self.server, 'ci_job': self.job,
                                     'ci_agent': self.agent, **labels})})
        return {'resourceMetrics': [{'resource': self.resource(), 'scopeMetrics': [{'metrics': [
            {'name': n, 'gauge': {'dataPoints': pts}} for n, pts in by_name.items()]}]}]}


def post(path, body):
    request = urllib.request.Request(OTLP + path, json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status
        except urllib.error.URLError as e:
            if attempt == 3:
                raise SystemExit(f'cannot reach the OTLP endpoint {OTLP}: {e}')
            time.sleep(2 ** attempt)


def load(cache, server, job, number):
    directory = os.path.join(cache, server, job, str(number))
    with open(os.path.join(directory, 'build.json')) as f:
        info = json.load(f)
    report = None
    path = os.path.join(directory, 'testReport.json.gz')
    if os.path.exists(path):
        try:
            with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
                report = json.load(f)
        except json.JSONDecodeError:
            pass  # build.ros2.org can return an HTML bot-challenge page with 200 instead of a 404
    return info, report, gzip.open(os.path.join(directory, 'console.log.gz'), 'rt',
                                   encoding='utf-8', errors='replace')


def capped_logs(logs):
    """logs, sorted by time, with a hard ceiling on line count (see MAX_LOG_LINES)."""
    logs = sorted(logs, key=lambda r: r[0])
    if len(logs) <= MAX_LOG_LINES:
        return logs
    half = MAX_LOG_LINES // 2
    head, tail = logs[:half], logs[-half:]
    marker = (head[-1][0], 'ERROR', f'... {len(logs) - 2 * half} lines omitted (console log '
             f'exceeded {MAX_LOG_LINES} lines; see the Jenkins build for the full log) ...', {})
    return head + [marker] + tail


def export_build(builder, dry_run, metrics_only=False, traces_only=False, logs_only=False):
    tests = sum(1 for s in builder.spans if s['name'].startswith('test '))
    print(f'  {len(builder.spans)} spans ({tests} test), {len(builder.logs)} logs, '
          f'{len(builder.metrics)} metric points, result {builder.info.get("result")}, '
          f'agent {builder.info.get("builtOn")}')
    if dry_run:
        return
    if metrics_only:
        post('/v1/metrics', builder.otlp_metrics())
        return
    if logs_only:
        logs = capped_logs(builder.logs)
        for i in range(0, len(logs), BATCH):
            post('/v1/logs', builder.otlp_logs(logs[i:i + BATCH]))
            time.sleep(LOG_POST_DELAY)
        return
    for i in range(0, len(builder.spans), BATCH):
        post('/v1/traces', builder.otlp_traces(builder.spans[i:i + BATCH]))
    if traces_only:
        return
    logs = capped_logs(builder.logs)
    for i in range(0, len(logs), BATCH):
        post('/v1/logs', builder.otlp_logs(logs[i:i + BATCH]))
        time.sleep(LOG_POST_DELAY)
    post('/v1/metrics', builder.otlp_metrics())


def grafana(path):
    with urllib.request.urlopen(f'{GRAFANA}/{path}', timeout=30) as response:
        return json.load(response)


def verify(builders, since, traces=True):
    """The Collector answers 200 before the backends accept data, so check what actually arrived."""
    time.sleep(10)
    ok = True
    for b in builders if traces else []:
        try:
            trace = grafana(f'tempo/api/traces/{b.trace_id}')
            stored = sum(len(s['spans']) for batch in trace['batches'] for s in batch['scopeSpans'])
        except urllib.error.URLError:
            stored = 0
        print(f'  #{b.number}: Tempo has {stored}/{len(b.spans)} spans')
        ok &= stored == len(b.spans)
    logs = subprocess.run(['docker', 'compose', 'logs', '--no-color', '--since', since,
                           'otel-collector', 'prometheus', 'loki', 'tempo'],
                          capture_output=True, text=True,
                          cwd=os.path.dirname(os.path.abspath(__file__))).stdout
    # Deliberately excludes "Rejecting data"/"sending queue is full"/"rate limit exceeded": those
    # are transient backpressure that the Collector's own retry_on_failure resolves within its
    # (default 300s) retry window, not real loss. Only count terminal, unretryable failures.
    dropped = [l for l in logs.splitlines() if re.search(
        r'Dropping data|out of bounds|too old|entry too far behind|no schema config found', l, re.I)]
    print(f'  backend rejections in logs: {len(dropped)}')
    for line in dropped[:3]:
        print('   ', line[:240])
    return ok and not dropped


def main():
    lock = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache', '.export.lock')
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        raise SystemExit(f'another export is running (or crashed); delete {lock} if it is stale')
    try:
        _main()
    finally:
        os.remove(lock)


def _main():
    global PKG_MIN_SECONDS, TEST_MIN_SECONDS
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--server', default='ci')
    parser.add_argument('--job', nargs='*', default=[], help='job names (default with --every-job: all cached)')
    parser.add_argument('--every-job', action='store_true', help='export every job found in the cache')
    parser.add_argument('--max-age-days', type=int, default=120,
                        help='skip jobs whose newest build is older than this (Prometheus rejects old samples)')
    parser.add_argument('--include-stale', action='store_true', help='ignore --max-age-days')
    parser.add_argument('--builds', type=int, nargs='*', default=[])
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--metrics-only', action='store_true',
                        help='re-send only metrics (safe to repeat); skips traces, logs, state')
    parser.add_argument('--logs-only', action='store_true',
                        help='re-send only logs (wipe Loki first); verifies, then marks builds exported')
    parser.add_argument('--traces-only', action='store_true',
                        help='re-send only traces (wipe Tempo first); skips logs, metrics, state')
    parser.add_argument('--pkg-min-seconds', type=float, default=PKG_MIN_SECONDS)
    parser.add_argument('--test-min-seconds', type=float, default=TEST_MIN_SECONDS)
    parser.add_argument('--force', action='store_true',
                        help='re-export; Loki rejects old entries in an existing stream')
    parser.add_argument('--cache', default=os.path.join(os.path.dirname(__file__), 'cache'))
    args = parser.parse_args()
    PKG_MIN_SECONDS, TEST_MIN_SECONDS = args.pkg_min_seconds, args.test_min_seconds

    server_root = os.path.join(args.cache, args.server)
    jobs = list(args.job)
    if args.every_job:
        jobs += sorted(j for j in os.listdir(server_root)
                       if os.path.isdir(os.path.join(server_root, j)) and j not in jobs)
    if not jobs:
        raise SystemExit('give --job and/or --every-job')
    state_path = os.path.join(args.cache, 'exported.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    only_one_signal = args.metrics_only or args.traces_only
    failed_jobs = []
    for job in jobs:
        print(f'== {job}')
        root = os.path.join(server_root, job)
        numbers = args.builds or sorted(int(n) for n in os.listdir(root) if n.isdigit())
        newest = max((json.load(open(os.path.join(root, str(n), 'build.json')))['timestamp']
                      for n in numbers if os.path.exists(os.path.join(root, str(n), 'build.json'))),
                     default=0)
        age_days = (time.time() * 1000 - newest) / 86_400_000
        if newest and age_days > args.max_age_days and not args.include_stale:
            print(f'{job}: newest build is {age_days:.0f} days old, skipping (stale job; '
                  '--include-stale to override)')
            continue
        started = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        builders = []
        for number in numbers:
            key = f'{args.server}/{job}/{number}'
            if key in state and not (args.force or args.dry_run or only_one_signal or args.logs_only):
                print(f'{job} #{number}: already exported, skipping')
                continue
            build_path = os.path.join(root, str(number), 'build.json')
            if not os.path.exists(build_path):
                print(f'{job} #{number}: incomplete in cache (no build.json), skipping')
                continue
            build_age_days = (time.time() * 1000 - json.load(open(build_path))['timestamp']) / 86_400_000
            if build_age_days > args.max_age_days and not args.include_stale:
                print(f'{job} #{number}: {build_age_days:.0f} days old, skipping (stale build; '
                      '--include-stale to override; Prometheus rejects samples past its '
                      'out_of_order_time_window regardless)')
                continue
            print(f'{job} #{number}')
            try:
                info, report, console = load(args.cache, args.server, job, number)
                with console:
                    builder = Builder(args.server, job, number, info, console, report,
                                      prev=load_prev_repos(root, number), cache=args.cache)
                save_repos(root, number, builder.repos)
                export_build(builder, args.dry_run, args.metrics_only, args.traces_only, args.logs_only)
            except Exception as e:  # noqa: B902
                print(f'  SKIPPED, could not export ({type(e).__name__}: {e})')
                failed_jobs.append(f'{job} #{number}')
                continue
            builders.append(builder)
        if builders and not args.dry_run and not only_one_signal:
            ok = verify(builders, started, traces=not args.logs_only)
            if ok:
                for b in builders:
                    state[f'{b.server}/{b.job}/{b.number}'] = started
                with open(state_path, 'w') as f:
                    json.dump(state, f, indent=1)
            else:
                failed_jobs.append(f'{job} (verification failed; not marked exported)')
    if failed_jobs:
        print('problems:', *failed_jobs, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
