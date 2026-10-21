"""Run every Prometheus query in the provisioned dashboards against the local stack and report which
return errors or no data. Variables are substituted with sample values.

    python check_dashboards.py
"""

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAFANA = 'http://localhost:3000/api/datasources/proxy/uid/prometheus/api/v1'
SUBST = {'$job_a': 'nightly_win_rel', '$job_b': 'nightly_linux_release', '$build_n': '3955', '$job': 'nightly_win_rel', '$package': 'launch_testing_ros', '$build': '.+',
         '$__range': '30d', '$__interval': '1d', '$__rate_interval': '1d'}


def run(expr, instant, now):
    for key, value in SUBST.items():
        expr = expr.replace(key, value)
    if instant:
        query = {'query': expr, 'time': now}
        path = 'query'
    else:
        query = {'query': expr, 'start': now - 30 * 86400, 'end': now, 'step': 86400}
        path = 'query_range'
    url = f'{GRAFANA}/{path}?' + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        return None, e.read().decode()[:160]
    return len(data['data']['result']), ''


def main():
    now = int(time.time())
    problems = 0
    for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              'grafana', 'dashboards', '*.json'))):
        dashboard = json.load(open(path))
        print(dashboard['title'])
        for panel in dashboard['panels']:
            for target in panel.get('targets', []):
                if target.get('datasource', {}).get('type') != 'prometheus':
                    continue
                count, error = run(target['expr'], target.get('instant', False), now)
                status = 'ERROR ' + error if count is None else ('no data' if count == 0 else f'{count} series')
                if count is None or count == 0:
                    problems += 1
                print(f'  {panel["title"][:58]:58} {status}')
    print('problems:', problems)
    return problems


if __name__ == '__main__':
    sys.exit(1 if main() else 0)
