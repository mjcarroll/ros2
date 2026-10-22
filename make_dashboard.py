"""Generate the Grafana dashboards in grafana/dashboards/. Run after editing: python make_dashboard.py"""

import json
import os
import urllib.parse

PROM = {'type': 'prometheus', 'uid': 'prometheus'}
LOKI = {'type': 'loki', 'uid': 'loki'}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'grafana', 'dashboards')
J = 'ci_job="$job"'
RESULT_COLORS = {'SUCCESS': 'green', 'UNSTABLE': 'yellow', 'FAILURE': 'orange', 'ABORTED': 'red'}


def result_overrides():
    return [{'matcher': {'id': 'byName', 'options': name}, 'properties': [
        {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': color}}]}
        for name, color in RESULT_COLORS.items()]


class Panels:
    def __init__(self):
        self.items = []

    def add(self, panel):
        panel['id'] = len(self.items) + 1
        self.items.append(panel)

    @staticmethod
    def target(expr, legend='', instant=False, table=False, interval=None, ref='A'):
        t = {'datasource': PROM, 'expr': expr, 'legendFormat': legend, 'instant': instant,
             'range': not instant, 'format': 'table' if table else 'time_series', 'refId': ref}
        if interval:
            t['interval'] = interval
        return t

    def stat(self, title, expr, x, y, w, h, unit='short', description='', color='blue'):
        self.add({'type': 'stat', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(expr, instant=True)], 'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'fieldConfig': {'defaults': {'unit': unit, 'noValue': '0', 'color': {
                      'mode': 'fixed', 'fixedColor': color}}, 'overrides': []},
                  'options': {'colorMode': 'value', 'graphMode': 'none', 'textMode': 'value',
                              'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False}}})

    def points(self, title, expr, legend, x, y, w, h, unit, description='', scale=''):
        # One sample per build per series, so take the last sample in each step window.
        self.add({'type': 'timeseries', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(f'last_over_time({expr}[$__interval]){scale}', legend)],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'fieldConfig': {'defaults': {'unit': unit, 'custom': {
                      'drawStyle': 'points', 'pointSize': 9, 'showPoints': 'always', 'spanNulls': True}},
                      'overrides': []},
                  'options': {'legend': {'displayMode': 'table', 'placement': 'bottom'},
                              'tooltip': {'mode': 'multi'}}})

    def bars(self, title, expr, legend, x, y, w, h, unit, description='', interval=None, scale='',
             window='$__interval', colors=None):
        self.add({'type': 'timeseries', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(f'{expr.replace("@W", window)}{scale}', legend,
                                          interval=interval)],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'fieldConfig': {'defaults': {'unit': unit, 'custom': {
                      'drawStyle': 'bars', 'fillOpacity': 80, 'lineWidth': 1, 'barAlignment': 0,
                      'stacking': {'mode': 'normal', 'group': 'A'}}}, 'overrides': colors or []},
                  'options': {'legend': {'displayMode': 'table', 'placement': 'bottom'},
                              'tooltip': {'mode': 'multi'}}})

    def bargauge(self, title, expr, legend, x, y, w, h, unit='s', description='', maximum=None):
        defaults = {'unit': unit, 'min': 0}
        if maximum is not None:
            defaults['max'] = maximum
        self.add({'type': 'bargauge', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(expr, legend, instant=True)],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'fieldConfig': {'defaults': defaults, 'overrides': []},
                  'options': {'orientation': 'horizontal', 'displayMode': 'gradient',
                              'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False}}})

    def pie(self, title, expr, legend, x, y, w, h, description=''):
        self.add({'type': 'piechart', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(expr, legend, instant=True)],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'fieldConfig': {'defaults': {'unit': 'short'}, 'overrides': result_overrides()},
                  'options': {'legend': {'displayMode': 'table', 'placement': 'right',
                                         'values': ['value']},
                              'pieType': 'donut', 'displayLabels': ['name'],
                              'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False}}})

    def table(self, title, expr, x, y, w, h, columns, rename, description='', sort=None,
              trace_link_column=False, value_name='Value', unit=None, extra_overrides=None):
        defaults = {'unit': unit} if unit else {}
        overrides = list(extra_overrides or [])
        if trace_link_column:
            overrides.append({'matcher': {'id': 'byName', 'options': 'Open trace'}, 'properties': [
                {'id': 'links', 'value': [{'title': 'Open build timeline', 'url': trace_link(),
                                           'targetBlank': True}]},
                {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
                {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'blue'}}]})
        transformations = [{'id': 'organize', 'options': {
            'excludeByName': {k: True for k in ('Time', '__name__', 'job')} | {
                k: True for k in ('ci_job', 'ci_agent', 'ci_build', 'ci_trace_id', 'ci_state',
                                  'ci_prev_build', 'ci_status', 'ci_first_failed_build')
                if k not in columns},
            'renameByName': {**rename, 'Value #A': value_name, 'Value': value_name},
            'indexByName': {c: i for i, c in enumerate(columns)}}}]
        if sort:
            transformations.append({'id': 'sortBy', 'options': {'sort': [{'field': sort[0],
                                                                          'desc': sort[1]}]}})
        self.add({'type': 'table', 'title': title, 'description': description, 'datasource': PROM,
                  'targets': [self.target(expr, instant=True, table=True)],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h}, 'transformations': transformations,
                  'fieldConfig': {'defaults': defaults, 'overrides': overrides},
                  'options': {'showHeader': True}})

    def logs(self, title, expr, x, y, w, h):
        self.add({'type': 'logs', 'title': title, 'datasource': LOKI,
                  'targets': [{'datasource': LOKI, 'queryType': 'range', 'refId': 'A', 'expr': expr}],
                  'gridPos': {'x': x, 'y': y, 'w': w, 'h': h},
                  'options': {'showTime': True, 'wrapLogMessage': True, 'sortOrder': 'Descending',
                              'enableLogDetails': True}})


def trace_link():
    marker = 'TRACEIDMARKER'
    prefix = '/explore?schemaVersion=1&orgId=1&panes=' + urllib.parse.quote(json.dumps({'a': {
        'datasource': 'tempo', 'queries': [{
            'refId': 'A', 'datasource': {'type': 'tempo', 'uid': 'tempo'}, 'queryType': 'traceql',
            'query': marker}], 'range': {'from': 'now-30d', 'to': 'now'}}}, separators=(',', ':')),
        safe='')
    before, after = prefix.split(marker)
    return before + '${__value.raw}' + after


def result_count(result=None):
    sel = J + (f',ci_result="{result}"' if result else '')
    return f'count(last_over_time(ci_build_result{{{sel}}}[$__range])) or vector(0)'


def variables(package=False, job=True):
    items = [] if not job else [{'name': 'job', 'label': 'Job', 'type': 'query', 'datasource': PROM, 'refresh': 2,
              'query': {'query': 'label_values(ci_build_duration_seconds, ci_job)', 'refId': 'job'},
              'current': {'text': 'nightly_win_rel', 'value': 'nightly_win_rel'}, 'sort': 1}]
    if package:
        items.append({'name': 'package', 'label': 'Package', 'type': 'query', 'datasource': PROM,
                      'refresh': 2, 'query': {'query': f'label_values(ci_package_duration_seconds{{{J}}}, '
                                                       'ci_package)', 'refId': 'package'},
                      'current': {'text': 'launch_testing_ros', 'value': 'launch_testing_ros'},
                      'sort': 1})
        items.append({'name': 'build', 'label': 'Build (regex)', 'type': 'textbox', 'query': '.+',
                      'current': {'text': '.+', 'value': '.+'}})
    return items


def query_var(name, label, expr, default, sort=1):
    return {'name': name, 'label': label, 'type': 'query', 'datasource': PROM, 'refresh': 2,
            'query': {'query': expr, 'refId': name}, 'sort': sort,
            'current': {'text': default, 'value': default}}


def write(uid, title, panels, package=False, description='', job=True, extra_vars=None):
    dashboard = {
        'uid': uid, 'title': title, 'description': description, 'schemaVersion': 39, 'version': 3,
        # Browser-local, not UTC: these jobs fire around 04:00 UTC (~11pm US-Central), so a UTC
        # label puts last night's build under today's date and makes it look not-yet-run.
        'timezone': 'browser', 'time': {'from': 'now-30d', 'to': 'now'}, 'tags': ['ros2', 'ci'],
        'panels': panels.items, 'templating': {'list': variables(package, job) + (extra_vars or [])},
        'links': [{'type': 'dashboards', 'tags': ['ros2'], 'title': 'ROS 2 CI dashboards',
                   'asDropdown': True, 'includeVars': True, 'keepTime': True}]}
    with open(os.path.join(OUT, f'{uid}.json'), 'w') as f:
        json.dump(dashboard, f, indent=1)
    print('wrote', uid, len(panels.items), 'panels')


BUILD_COLUMNS = ['ci_build', 'ci_result', 'ci_agent', 'ci_trace_id']
BUILD_RENAME = {'ci_build': 'Build', 'ci_trace_id': 'Open trace', 'ci_result': 'Result',
                'ci_agent': 'Agent', 'ci_package': 'Package', 'ci_phase': 'Phase', 'ci_state': 'State'}


def overview():
    p = Panels()
    p.points('Build duration (minutes)', f'ci_build_duration_seconds{{{J}}}',
             '#{{ci_build}} {{ci_result}} on {{ci_agent}}', 0, 0, 12, 8, 'm',
             'One point per build at its end time. 600 min is the Jenkins timeout.', ' / 60')
    p.points('Failed tests per build', f'ci_tests_failed{{{J}}}', '#{{ci_build}} on {{ci_agent}}',
             12, 0, 12, 8, 'short')
    p.table('Builds - click "Open trace" for the build timeline',
            f'last_over_time(ci_build_result{{{J}}}[$__range])', 0, 8, 24, 8, BUILD_COLUMNS,
            BUILD_RENAME, 'Each row is one build. The trace shows the Docker image build, setup, '
            'the colcon build and test phases, and the slow or failed packages and tests.',
            sort=('Build', True), trace_link_column=True)
    p.bargauge('Slowest packages in the selected time range',
               f'topk(15, max by (ci_package, ci_phase) (max_over_time(ci_package_duration_seconds'
               f'{{{J}}}[$__range])))', '{{ci_package}} ({{ci_phase}})', 0, 16, 12, 10)
    p.points('Duration over time: $package',
             f'ci_package_duration_seconds{{{J}, ci_package="$package"}}',
             '{{ci_phase}} {{ci_state}} on {{ci_agent}}', 12, 16, 12, 10, 's',
             'Pick the package at the top. State Unfinished means the build ended while it was '
             'still running (a hang, cut off by the timeout).')
    p.table('Failed or unfinished packages (hangs) - click "Open trace"',
            f'max_over_time(ci_package_duration_seconds{{{J}, ci_state!="Finished"}}[$__range])',
            0, 26, 24, 6, ['ci_build', 'ci_package', 'ci_phase', 'ci_state', 'ci_agent', 'ci_trace_id'],
            BUILD_RENAME, 'Packages that did not finish normally. Unfinished = still running at '
            'the timeout.', sort=('Build', True), trace_link_column=True, value_name='Seconds', unit='s')
    p.logs('Errors and failed tests (console and test reports)',
           '{service_namespace="$job", service_instance_id=~"$build"}', 0, 32, 24, 12)
    write('ros2-ci-overview', 'ROS 2 CI overview', p, package=True,
          description='Builds, hangs and slow packages; click a trace id to open a build timeline.')


def health():
    p = Panels()
    w = 4
    p.stat('Builds', result_count(), 0, 0, w, 4)
    p.stat('Green (SUCCESS)', result_count('SUCCESS'), w, 0, w, 4, color='green')
    p.stat('Unstable (test failures)', result_count('UNSTABLE'), 2 * w, 0, w, 4, color='yellow')
    p.stat('Failed (FAILURE)', result_count('FAILURE'), 3 * w, 0, w, 4, color='orange')
    p.stat('Aborted (timeout / hang)', result_count('ABORTED'), 4 * w, 0, w, 4, color='red',
           description='Builds stopped by the 600 minute limit.')
    p.stat('Median build duration',
           f'quantile(0.5, last_over_time(ci_build_duration_seconds{{{J}}}[$__range]))', 5 * w, 0, w, 4,
           unit='s')
    p.pie('Builds by result',
          f'count by (ci_result)(last_over_time(ci_build_result{{{J}}}[$__range]))', '{{ci_result}}',
          0, 4, 8, 9)
    p.bars('Builds per day by result',
           f'sum by (ci_result)(count_over_time(ci_build_result{{{J}}}[@W]))', '{{ci_result}}', 8, 4, 16, 9,
           'short', window='1d', interval='1d', colors=result_overrides())
    p.stat('Agent-hours lost to aborted builds',
           f'sum(last_over_time(ci_build_duration_seconds{{{J},ci_result="ABORTED"}}[$__range])) / 3600',
           0, 13, 6, 5, unit='h', color='red',
           description='Time spent on builds that ended at the timeout instead of finishing.')
    p.stat('Agent-hours, all builds',
           f'sum(last_over_time(ci_build_duration_seconds{{{J}}}[$__range])) / 3600', 6, 13, 6, 5,
           unit='h')
    p.points('Build duration (minutes)', f'ci_build_duration_seconds{{{J}}}',
             '#{{ci_build}} {{ci_result}}', 12, 13, 12, 9, 'm', scale=' / 60')
    p.points('Tests failed per build', f'ci_tests_failed{{{J}}}', '#{{ci_build}} {{ci_agent}}',
             0, 22, 12, 8, 'short')
    p.points('Tests passed per build', f'ci_tests_passed{{{J}}}', '#{{ci_build}} {{ci_agent}}',
             12, 22, 12, 8, 'short', 'A sudden drop means tests stopped running, for example after a '
             'hang or a crash.')
    p.table('Builds - click "Open trace"', f'last_over_time(ci_build_result{{{J}}}[$__range])',
            0, 30, 24, 8, BUILD_COLUMNS, BUILD_RENAME, sort=('Build', True), trace_link_column=True)
    write('ros2-ci-health', 'CI health', p,
          description='Result mix, hung builds and agent-hours lost, test volume.')


def time_breakdown():
    p = Panels()
    p.bars('Time per phase of each build (minutes)',
           f'last_over_time(ci_section_duration_seconds{{{J},ci_kind="section"}}[@W])', '{{ci_section}}',
           0, 0, 12, 9, 'm', 'Top-level sections of the job: the Docker image build and the run.',
           scale=' / 60')
    p.bars('Inside the run (minutes)',
           f'last_over_time(ci_section_duration_seconds{{{J},ci_kind="subsection"}}[@W])',
           '{{ci_section}}', 12, 0, 12, 9, 'm', 'Repository import, colcon build and colcon test, and '
           'the smaller setup steps.', scale=' / 60')
    p.bargauge('Average time per phase in range',
               f'avg by (ci_section)(last_over_time(ci_section_duration_seconds{{{J}}}[$__range]))',
               '{{ci_section}}', 0, 9, 12, 10, description='Averaged over the builds in the range.')
    p.bargauge('Slowest packages: colcon build',
               f'topk(12, avg by (ci_package)(avg_over_time(ci_package_duration_seconds{{{J},'
               f'ci_phase="build",ci_state="Finished"}}[$__range])))', '{{ci_package}}', 12, 9, 12, 10)
    p.bargauge('Slowest packages: colcon test',
               f'topk(12, avg by (ci_package)(avg_over_time(ci_package_duration_seconds{{{J},'
               f'ci_phase="test",ci_state="Finished"}}[$__range])))', '{{ci_package}}', 0, 19, 12, 10)
    p.table('Packages getting slower: last 7 days vs the 7 days before',
            f'topk(15, avg by (ci_package, ci_phase)(avg_over_time(ci_package_duration_seconds{{{J},'
            f'ci_state="Finished"}}[7d])) - avg by (ci_package, ci_phase)(avg_over_time('
            f'ci_package_duration_seconds{{{J},ci_state="Finished"}}[7d] offset 7d)))',
            12, 19, 12, 10, ['ci_package', 'ci_phase'], BUILD_RENAME,
            'Average duration over the last 7 days minus the 7 days before that, ignoring the time '
            'picker. Needs two weeks of builds.', sort=('Seconds slower', True), value_name='Seconds slower',
            unit='s')
    write('ros2-ci-time', 'Where the time goes', p, description='Phase and package timing.')


def tests():
    p = Panels()
    cls = {'ci_test_class': 'Suite', 'ci_test': 'Test', 'ci_status': 'Status'}
    p.table('Most frequently failing tests',
            f'topk(25, count by (ci_test_class, ci_test)(last_over_time(ci_test_failure{{{J}}}[$__range])))',
            0, 0, 14, 11, ['ci_test_class', 'ci_test'], cls, 'Number of builds in the range in which '
            'the test failed.', sort=('Builds failed', True), value_name='Builds failed')
    p.bargauge('Failures by suite',
               f'topk(12, count by (ci_test_class)(last_over_time(ci_test_failure{{{J}}}[$__range])))',
               '{{ci_test_class}}', 14, 0, 10, 11, unit='short')
    p.table('Failing the longest (builds in a row)',
            f'topk(25, max by (ci_test_class, ci_test)(last_over_time(ci_test_failure{{{J}}}[$__range])))',
            0, 11, 14, 11, ['ci_test_class', 'ci_test'], cls, 'Jenkins "age": consecutive builds the '
            'test has been failing as of the latest build in the range.', sort=('Age', True),
            value_name='Age')
    p.table('Flaky candidates (failed in some builds, not all)',
            f'(count by (ci_test_class, ci_test)(last_over_time(ci_test_failure{{{J}}}[$__range]))) '
            f'< scalar(count(last_over_time(ci_build_result{{{J}}}[$__range])))',
            14, 11, 10, 11, ['ci_test_class', 'ci_test'], cls, 'Failed in at least one but not every '
            'build in the range.', sort=('Builds failed', True), value_name='Builds failed')
    p.points('Tests failed per build', f'ci_tests_failed{{{J}}}', '#{{ci_build}} {{ci_agent}}',
             0, 22, 12, 8, 'short')
    p.points('Tests skipped per build', f'ci_tests_skipped{{{J}}}', '#{{ci_build}} {{ci_agent}}',
             12, 22, 12, 8, 'short')
    write('ros2-ci-tests', 'Tests and flakiness', p, description='Failing and flaky tests.')


def agents():
    p = Panels()
    p.bargauge('Bad build rate per agent (FAILURE or ABORTED)',
               f'100 * count by (ci_agent)(last_over_time(ci_build_result{{{J},'
               f'ci_result=~"ABORTED|FAILURE"}}[$__range])) / count by (ci_agent)(last_over_time('
               f'ci_build_result{{{J}}}[$__range]))', '{{ci_agent}}', 0, 0, 12, 9, unit='percent',
               description='Agents are re-imaged, so ids change over time; look for repeat offenders.',
               maximum=100)
    p.bargauge('Builds per agent', f'count by (ci_agent)(last_over_time(ci_build_result{{{J}}}[$__range]))',
               '{{ci_agent}}', 12, 0, 12, 9, unit='short')
    p.bargauge('Average build duration per agent',
               f'avg by (ci_agent)(last_over_time(ci_build_duration_seconds{{{J}}}[$__range]))',
               '{{ci_agent}}', 0, 9, 12, 9)
    p.table('Results per agent',
            f'count by (ci_agent, ci_result)(last_over_time(ci_build_result{{{J}}}[$__range]))',
            12, 9, 12, 9, ['ci_agent', 'ci_result'], BUILD_RENAME, sort=('Builds', True),
            value_name='Builds')
    p.points('Build duration by agent (minutes)', f'ci_build_duration_seconds{{{J}}}',
             '{{ci_agent}} #{{ci_build}} {{ci_result}}', 0, 18, 24, 9, 'm', scale=' / 60')
    write('ros2-ci-agents', 'Agents', p, description='Per-agent results and durations.')


def matrix():
    p = Panels()
    # Scoped to ci.ros2.org: build.ros2.org's infra/check jobs share the same metric names but
    # are not nightlies, and would otherwise show up mixed into this scorecard. ci_* jobs are
    # on-demand (PR-triggered via ci_launcher); test_* jobs validate those job definitions
    # rather than running on a schedule. Neither belongs in the nightly scorecard.
    N = 'ci_server="ci", ci_job!~"ci_.*|test_.*"'
    count = lambda extra='': (  # noqa: E731
        f'count by (ci_job)(last_over_time(ci_build_result{{{N}{extra}}}[$__range]))')
    queries = [
        ('A', count(), 'Builds'),
        ('B', count(',ci_result="SUCCESS"'), 'Green'),
        ('C', count(',ci_result="UNSTABLE"'), 'Unstable'),
        ('D', count(',ci_result="FAILURE"'), 'Failed'),
        ('E', count(',ci_result="ABORTED"'), 'Aborted'),
        ('F', f'quantile by (ci_job)(0.5, last_over_time(ci_build_duration_seconds{{{N}}}'
              '[$__range])) / 60', 'Median min'),
        ('G', f'sum by (ci_job)(last_over_time(ci_build_duration_seconds{{{N},'
              'ci_result="ABORTED"}[$__range])) / 3600', 'Hours lost to aborts'),
    ]
    threshold = lambda name, color: {  # noqa: E731
        'matcher': {'id': 'byName', 'options': name}, 'properties': [
            {'id': 'custom.cellOptions', 'value': {'type': 'color-background'}},
            {'id': 'thresholds', 'value': {'mode': 'absolute', 'steps': [
                {'color': 'transparent', 'value': None}, {'color': color, 'value': 1}]}}]}
    p.add({'type': 'table', 'title': 'Nightly scorecard', 'datasource': PROM,
           'description': 'One row per job over the selected range. Green/yellow/orange/red columns are '
                          'counts of builds by result; Aborted means the 600 minute limit was hit.',
           'targets': [Panels.target(expr, instant=True, table=True, ref=ref) for ref, expr, _ in queries],
           'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 11},
           'transformations': [
               {'id': 'joinByField', 'options': {'byField': 'ci_job', 'mode': 'outer'}},
               {'id': 'organize', 'options': {
                   'excludeByName': {f'Time {i}': True for i in range(1, 8)} | {'Time': True},
                   'renameByName': {'ci_job': 'Job', **{f'Value #{ref}': name for ref, _, name in queries}},
                   'indexByName': {'ci_job': 0, **{f'Value #{ref}': i + 1
                                                   for i, (ref, _, _) in enumerate(queries)}}}},
               {'id': 'sortBy', 'options': {'sort': [{'field': 'Job', 'desc': False}]}}],
           'fieldConfig': {'defaults': {'decimals': 1, 'noValue': '0'}, 'overrides': [
               threshold('Aborted', 'red'), threshold('Failed', 'orange'),
               {'matcher': {'id': 'byName', 'options': 'Green'}, 'properties': [
                   {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
                   {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'green'}}]}]},
           'options': {'showHeader': True}})
    worst = ' or '.join(
        f'last_over_time(ci_build_result{{{N},ci_result="{r}"}}[@W]) * {n}'
        for n, r in ((4, 'ABORTED'), (3, 'FAILURE'), (2, 'UNSTABLE'), (1, 'SUCCESS')))
    p.add({'type': 'status-history', 'title': 'Worst result per job per day', 'datasource': PROM,
           'description': 'Green = SUCCESS, yellow = UNSTABLE, orange = FAILURE, red = ABORTED. '
                          'A day with several builds shows the worst one.',
           'targets': [Panels.target(f'max by (ci_job)({worst.replace("@W", "1d")})', '{{ci_job}}',
                                     interval='1d')],
           'gridPos': {'x': 0, 'y': 11, 'w': 24, 'h': 12},
           'fieldConfig': {'defaults': {'custom': {'fillOpacity': 85, 'lineWidth': 1},
                                        'color': {'mode': 'thresholds'},
                                        'mappings': [{'type': 'value', 'options': {
                                            '1': {'text': 'SUCCESS', 'color': 'green', 'index': 0},
                                            '2': {'text': 'UNSTABLE', 'color': 'yellow', 'index': 1},
                                            '3': {'text': 'FAILURE', 'color': 'orange', 'index': 2},
                                            '4': {'text': 'ABORTED', 'color': 'red', 'index': 3}}}],
                                        'thresholds': {'mode': 'absolute', 'steps': [
                                            {'color': 'text', 'value': None}]}}, 'overrides': []},
           'options': {'showValue': 'never', 'rowHeight': 0.9, 'legend': {'showLegend': False},
                       'tooltip': {'mode': 'single'}}})
    p.bargauge('Hours lost to aborted builds, by job',
               f'sum by (ci_job)(last_over_time(ci_build_duration_seconds{{{N},ci_result="ABORTED"}}'
               '[$__range])) / 3600', '{{ci_job}}', 0, 23, 12, 9, unit='h',
               description='Time spent in builds that ended at the 600 minute limit.')
    p.bars('Aborted builds per day, by job',
           f'sum by (ci_job)(count_over_time(ci_build_result{{{N},ci_result="ABORTED"}}[@W]))',
           '{{ci_job}}', 12, 23, 12, 9, 'short', window='1d', interval='1d')
    p.table('Hangs and cut-off packages across all jobs - click "Open trace"',
            f'max_over_time(ci_package_duration_seconds{{{N},ci_state="Unfinished"}}[$__range])',
            0, 32, 24, 8, ['ci_job', 'ci_build', 'ci_package', 'ci_phase', 'ci_agent', 'ci_trace_id'],
            {**BUILD_RENAME, 'ci_job': 'Job'}, 'Packages still running when a build ended. A package that '
            'shows up in several jobs points at the package, not the platform.', sort=('Build', True),
            trace_link_column=True, value_name='Seconds', unit='s')
    p.table('Most frequently failing tests across all jobs',
            f'topk(25, count by (ci_test_class, ci_test)(last_over_time(ci_test_failure{{{N}}}[$__range])))',
            0, 40, 24, 10, ['ci_test_class', 'ci_test'],
            {'ci_test_class': 'Suite', 'ci_test': 'Test'}, 'Number of builds, over every job, in which the '
            'test failed.', sort=('Builds failed', True), value_name='Builds failed')
    p.points('Build duration by job (hours)', f'ci_build_duration_seconds{{{N}}}',
             '{{ci_job}} #{{ci_build}}', 0, 50, 24, 10, 'h',
             'Points at 10 hours are the aborted builds.', scale=' / 3600')
    write('ros2-ci-matrix', 'All nightlies', p, job=False,
          description='Every nightly job side by side: scorecard, worst result per day, hours lost to hangs.')


def ondemand():
    p = Panels()
    # ci_* jobs run on demand, fanned out by ci_launcher per PR comment/click; test_ci_* are the
    # same fan-out for validating the job definitions themselves. Neither is a scheduled nightly.
    OD = 'ci_server="ci", ci_job=~"ci_.*|test_ci_.*"'
    ONDEMAND_RENAME = {**BUILD_RENAME, 'ci_job': 'Job', 'ci_requested_by': 'Requested by',
                       'ci_target_package': 'Target package(s)'}
    count = lambda extra='': (  # noqa: E731
        f'count by (ci_job)(last_over_time(ci_build_result{{{OD}{extra}}}[$__range]))')
    queries = [
        ('A', count(), 'Builds'),
        ('B', count(',ci_result="SUCCESS"'), 'Green'),
        ('C', count(',ci_result="UNSTABLE"'), 'Unstable'),
        ('D', count(',ci_result="FAILURE"'), 'Failed'),
        ('E', count(',ci_result="ABORTED"'), 'Aborted'),
    ]
    threshold = lambda name, color: {  # noqa: E731
        'matcher': {'id': 'byName', 'options': name}, 'properties': [
            {'id': 'custom.cellOptions', 'value': {'type': 'color-background'}},
            {'id': 'thresholds', 'value': {'mode': 'absolute', 'steps': [
                {'color': 'transparent', 'value': None}, {'color': color, 'value': 1}]}}]}
    p.add({'type': 'table', 'title': 'On-demand scorecard', 'datasource': PROM,
           'description': 'One row per ci_*/test_ci_* job over the selected range.',
           'targets': [Panels.target(expr, instant=True, table=True, ref=ref) for ref, expr, _ in queries],
           'gridPos': {'x': 0, 'y': 0, 'w': 24, 'h': 10},
           'transformations': [
               {'id': 'joinByField', 'options': {'byField': 'ci_job', 'mode': 'outer'}},
               {'id': 'organize', 'options': {
                   'excludeByName': {f'Time {i}': True for i in range(1, 5)} | {'Time': True},
                   'renameByName': {'ci_job': 'Job', **{f'Value #{ref}': name for ref, _, name in queries}},
                   'indexByName': {'ci_job': 0, **{f'Value #{ref}': i + 1
                                                   for i, (ref, _, _) in enumerate(queries)}}}},
               {'id': 'sortBy', 'options': {'sort': [{'field': 'Job', 'desc': False}]}}],
           'fieldConfig': {'defaults': {'decimals': 1, 'noValue': '0'}, 'overrides': [
               threshold('Aborted', 'red'), threshold('Failed', 'orange'),
               {'matcher': {'id': 'byName', 'options': 'Green'}, 'properties': [
                   {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
                   {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'green'}}]}]},
           'options': {'showHeader': True}})
    p.bars('On-demand builds per day, by requester',
           f'sum by (ci_requested_by)(count_over_time(ci_build_result{{{OD}}}[@W]))',
           '{{ci_requested_by}}', 0, 10, 12, 9, 'short', window='1d', interval='1d',
           description='Who clicked "Build" on ci_launcher (or test_ci_launcher); resolved one hop '
                       'up for the ci_linux/ci_windows/... builds it fans out to.')
    p.bars('On-demand builds per day, by result',
           f'sum by (ci_result)(count_over_time(ci_build_result{{{OD}}}[@W]))',
           '{{ci_result}}', 12, 10, 12, 9, 'short', window='1d', interval='1d',
           colors=result_overrides(),
           description='By job is omitted: ci_launcher fans out to the same ~4 jobs every time in '
                       'near-equal numbers, so it tracks total volume rather than anything that '
                       'varies day to day.')
    p.table('Builds - click "Open trace" for the build timeline',
            f'last_over_time(ci_build_result{{{OD}}}[$__range])', 0, 19, 24, 12,
            ['ci_job', 'ci_build', 'ci_result', 'ci_requested_by', 'ci_target_package', 'ci_trace_id'],
            ONDEMAND_RENAME, 'One row per on-demand build. Target package(s) comes from '
            '--packages-above(-and-dependencies) in CI_BUILD_ARGS.', sort=('Build', True),
            trace_link_column=True)
    write('ros2-ci-ondemand', 'On-demand builds', p, job=False,
          description='ci_*/test_ci_* builds that ci_launcher fans out per PR: trends, who requested '
                      'them, their scope, and their results.')


BUILD_VAR = query_var('build_n', 'Build', 'label_values(ci_build_result{ci_job="$job"}, ci_build)', '', sort=4)
HIST = '[$__range]'


def regressions():
    p = Panels()
    p.table('Tests failing in build $build_n, and since which build',
            'last_over_time(ci_test_failure{ci_job="$job", ci_build="$build_n"}[$__range])', 0, 0, 24, 11,
            ['ci_first_failed_build', 'ci_test_class', 'ci_test', 'ci_status'],
            {'ci_first_failed_build': 'Failing since build', 'ci_test_class': 'Suite', 'ci_test': 'Test',
             'ci_status': 'Status'}, 'Newest regressions first. "Failing since" is the build in which the '
            'test first failed; pick that build above to see which repos changed in it.',
            sort=('Failing since build', True), value_name='Builds failing')
    link = {'matcher': {'id': 'byName', 'options': 'New'}, 'properties': [
        {'id': 'links', 'value': [{'title': 'Compare on GitHub', 'targetBlank': True,
                                   'url': '${__data.fields.URL}/compare/${__data.fields.Old}...${__value.raw}'}]},
        {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
        {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'blue'}}]}
    p.table('Repos that changed since the previous build, in build $build_n',
            'last_over_time(ci_repo_changed{ci_job="$job", ci_build="$build_n"}[$__range])', 0, 11, 24, 10,
            ['ci_repo', 'ci_old', 'ci_new', 'ci_prev_build', 'ci_url'],
            {'ci_repo': 'Repo', 'ci_old': 'Old', 'ci_new': 'New', 'ci_prev_build': 'Previous build',
             'ci_url': 'URL'}, 'Nightlies build the head of every repo, so a regression is some change '
            'in this list. Click New to open the compare view on GitHub.', sort=('Repo', False),
            value_name='Changed', extra_overrides=[link])
    p.bars('Repos changed per build', 'count by (ci_build)(last_over_time(ci_repo_changed{ci_job="$job"}[@W]))',
           '#{{ci_build}}', 0, 21, 12, 8, 'short', 'Builds with many changed repos are harder to bisect.')
    p.add({'type': 'status-history', 'title': 'Test history: the most frequently failing tests, build by build',
           'datasource': PROM, 'description': 'Red = failing in a build that day. A solid run is a real '
           'regression; scattered cells are flaky.',
           'targets': [Panels.target(
               'max by (ci_test)(last_over_time(ci_test_failure{ci_job="$job"}[1d]) and on (ci_test_class, '
               'ci_test) topk(15, count by (ci_test_class, ci_test)(last_over_time(ci_test_failure'
               '{ci_job="$job"}[$__range]))))', '{{ci_test}}', interval='1d')],
           'gridPos': {'x': 12, 'y': 21, 'w': 12, 'h': 8},
           'fieldConfig': {'defaults': {'custom': {'fillOpacity': 85, 'lineWidth': 1},
                                        'color': {'mode': 'thresholds'},
                                        'thresholds': {'mode': 'absolute', 'steps': [
                                            {'color': 'red', 'value': None}]}}, 'overrides': []},
           'options': {'showValue': 'never', 'rowHeight': 0.9, 'legend': {'showLegend': False}}})
    write('ros2-ci-regressions', 'Regressions: what changed', p, extra_vars=[BUILD_VAR],
          description='For a build: which tests failed since when, and which repos changed.')


def performance():
    p = Panels()
    x = 'ci_package_duration_seconds{ci_job="$job", ci_state="Finished"}'
    latest = f'max by (ci_package, ci_phase)(last_over_time({x}[3d]))'
    usual = f'quantile by (ci_package, ci_phase)(0.5, quantile_over_time(0.5, {x}[30d]))'
    p.table('Packages slower than their usual (latest 3 days vs the 30 day median)',
            f'topk(25, ({latest} / {usual} > 1.5) and ({latest} - {usual} > 30))', 0, 0, 12, 10,
            ['ci_package', 'ci_phase'], {'ci_package': 'Package', 'ci_phase': 'Phase'},
            'More than 1.5 times the median and at least 30 s slower. The value is the ratio.',
            sort=('Slower by', True), value_name='Slower by')
    p.bars('Agent-hours per day, by job', 'sum by (ci_job)(last_over_time(ci_build_duration_seconds[@W])) / 3600',
           '{{ci_job}}', 12, 0, 12, 10, 'h', 'Total build time across all jobs; hangs show as 10 hour builds.',
           window='1d', interval='1d')
    lint = 'cpplint.*|uncrustify|flake8|pep257|xmllint|copyright|lint_cmake|cppcheck|mypy|pyflakes|' \
           'pycodestyle|clang_tidy|clang_format|pclint|ament_.*'
    p.stat('Linters as a share of the test phase',
           f'100 * sum(last_over_time(ci_test_total_seconds{{ci_job="$job", ci_test=~"{lint}"}}[$__range])) / '
           'sum(last_over_time(ci_section_duration_seconds{ci_job="$job", ci_kind="subsection", '
           'ci_section="test"}[$__range]))', 0, 10, 6, 5, unit='percent', color='yellow',
           description='Time in linter tests (cpplint, flake8, xmllint ...) over test phase wall time. '
           'Tests run sequentially in CI, so this is close to the share of the phase.')
    p.bargauge('Test time by test name (average per build)',
               'topk(15, avg by (ci_test)(last_over_time(ci_test_total_seconds{ci_job="$job"}[$__range])))',
               '{{ci_test}}', 6, 10, 18, 10, description='Names that run in many packages, summed over '
               'the build. A linter near the top is a candidate to run once, not per package.')
    p.bargauge('Slowest individual tests (average)',
               'topk(15, avg by (ci_package, ci_test)(avg_over_time(ci_test_duration_seconds{ci_job="$job"}'
               '[$__range])))', '{{ci_package}}: {{ci_test}}', 0, 15, 12, 10)
    a = 'avg by (ci_package, ci_phase)(avg_over_time(ci_package_duration_seconds{ci_job="$job_a",ci_state="Finished"}[$__range]))'
    b = 'avg by (ci_package, ci_phase)(avg_over_time(ci_package_duration_seconds{ci_job="$job_b",ci_state="Finished"}[$__range]))'
    p.table('Biggest gap: $job_a minus $job_b, seconds per package',
            f'topk(20, {a} - {b})', 12, 15, 12, 10, ['ci_package', 'ci_phase'],
            {'ci_package': 'Package', 'ci_phase': 'Phase'}, 'Packages that cost the most extra time on the '
            'first job compared to the second, e.g. Windows against Linux.', sort=('Seconds slower', True),
            value_name='Seconds slower', unit='s')
    p.table('Slowest relative to $job_b: ratio, packages over 30 s',
            f'topk(20, ({a} / {b}) and ({a} > 30))', 0, 25, 24, 9, ['ci_package', 'ci_phase'],
            {'ci_package': 'Package', 'ci_phase': 'Phase'}, 'How many times slower the first job is.',
            sort=('Times slower', True), value_name='Times slower')
    write('ros2-ci-perf', 'Performance', p, extra_vars=[
        query_var('job_a', 'Compare', 'label_values(ci_build_duration_seconds, ci_job)', 'nightly_win_rel'),
        query_var('job_b', 'Against', 'label_values(ci_build_duration_seconds, ci_job)',
                  'nightly_linux_release')],
          description='Packages that got slower, test and linter cost, and platform comparison.')


def errors():
    p = Panels()
    sig = {'ci_signature': 'Error message (numbers and paths normalised)'}
    p.table('Rare messages in build $build_n (seen in at most 2 builds)',
            'last_over_time(ci_error_signature_count{ci_job="$job", ci_build="$build_n"}[$__range]) '
            'and on (ci_signature) (count by (ci_signature)(last_over_time('
            'ci_error_signature_count{ci_job="$job"}[$__range])) <= 2)',
            0, 0, 24, 10,
            ['ci_signature'], sig, 'Messages in this build that appear in at most two builds in the '
            'range: new failure modes or one-off problems. The first builds in a range look rarer.',
            sort=('Count', True),
            value_name='Count')
    p.table('Most frequent error messages in the time range',
            'topk(25, sum by (ci_signature)(last_over_time(ci_error_signature_count{ci_job="$job"}[$__range])))',
            0, 10, 24, 11, ['ci_signature'], sig, 'Counted from the console, before Loki collapses '
            'identical lines; only the 40 most common messages per build are kept.',
            sort=('Count', True), value_name='Count')
    p.bars('Error lines per build', 'sum by (ci_build)(last_over_time(ci_error_signature_count{ci_job="$job"}[@W]))',
           '#{{ci_build}}', 0, 21, 24, 8, 'short', 'A jump here usually means a new failure mode.')
    p.bars('Checkout-stage failures by cause',
           'sum by (ci_cause)(last_over_time(ci_checkout_failure{ci_job="$job"}[@W]))',
           '{{ci_cause}}', 0, 29, 24, 8, 'short',
           'Builds that never got past Jenkins\' own git checkout/submodule-update step (no "# BEGIN '
           'SECTION" ever ran), classified by cause: submodule_clone (a submodule failed to clone), '
           'network (a generic git/transport error on the main checkout), timeout (the checkout step '
           'itself was killed after its time limit with no more specific error seen).')
    p.table('Checkout-stage failures across all jobs in the range (ignores $job)',
            'count by (ci_job, ci_cause)(last_over_time(ci_checkout_failure[$__range]))',
            0, 37, 24, 10, ['ci_job', 'ci_cause'], {'ci_job': 'Job', 'ci_cause': 'Cause'},
            'Farm-wide view: submodule clone wedges and transient network errors during checkout are '
            'an infra problem, not a per-job one.', sort=('Count', True), value_name='Count')
    write('ros2-ci-errors', 'Errors', p, extra_vars=[BUILD_VAR],
          description='Recurring and new error messages from the console.')


SERVER_VAR = {'name': 'server', 'label': 'Server', 'type': 'custom', 'query': 'ci,build',
              'current': {'text': 'build', 'value': 'build'},
              'options': [{'text': s, 'value': s, 'selected': s == 'build'} for s in ('ci', 'build')]}


def buildfarm():
    # Queue depth, wait time and agent status, polled live by buildfarm.py (not reconstructed
    # from console logs: Jenkins keeps no queue history). check_agents / ci__infra_* job results
    # (ordinary builds, fetched like any other job) show up in "ROS 2 CI overview" / "CI health"
    # once $job is set to one of them, so they are not duplicated here.
    p = Panels()
    S = 'ci_server="$server"'
    w = 6
    p.stat('Agents', f'last_over_time(ci_agents_total{{{S}}}[$__range])', 0, 0, w, 4)
    p.stat('Agents offline', f'last_over_time(ci_agents_offline{{{S}}}[$__range])', w, 0, w, 4,
           color='red')
    p.stat('Queue depth now', f'last_over_time(ci_queue_depth{{{S}}}[$__range])', 2 * w, 0, w, 4)
    p.stat('Oldest queue wait', f'last_over_time(ci_queue_oldest_wait_seconds{{{S}}}[$__range])',
           3 * w, 0, w, 4, unit='s')
    p.points('Queue depth over time', f'ci_queue_depth{{{S}}}', 'queue depth', 0, 4, 12, 8, 'short')
    p.points('Oldest item wait (minutes)', f'ci_queue_oldest_wait_seconds{{{S}}}', 'oldest wait',
             12, 4, 12, 8, 'm', scale=' / 60')
    p.table('Agents (latest poll)', f'last_over_time(ci_agent_online{{{S}}}[$__range])',
            0, 12, 12, 10, ['ci_agent'], {'ci_agent': 'Agent'}, 'Online = 1, offline = 0 as of '
            'the last poll.', sort=('Agent', False), value_name='Online')
    p.bargauge('Executors per agent', f'last_over_time(ci_agent_executors{{{S}}}[$__range])',
               '{{ci_agent}}', 12, 12, 12, 10, unit='short')
    p.bargauge('Jobs waiting right now',
               f'topk(20, max by (ci_task)(last_over_time(ci_queue_item_wait_seconds{{{S}}}'
               '[$__range])))', '{{ci_task}}', 0, 22, 24, 10, unit='s',
               description='Empty if the queue was empty at the last poll.')
    write('ros2-ci-buildfarm', 'Build farm health', p, job=False, extra_vars=[SERVER_VAR],
          description='Live queue and agent status for ci.ros2.org and build.ros2.org, polled by '
          'buildfarm.py (point-in-time, not reconstructed from console logs).')


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    regressions()
    performance()
    errors()
    matrix()
    ondemand()
    overview()
    health()
    time_breakdown()
    tests()
    agents()
    buildfarm()
