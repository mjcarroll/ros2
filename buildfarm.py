"""Snapshot a Jenkins server's queue and agents right now and push the numbers as OTLP gauges.

    python buildfarm.py --server build
    python buildfarm.py --server ci --dry-run

Point-in-time only: Jenkins keeps no queue history, so there is nothing to backfill. Run this on
an interval (cron, Task Scheduler, refresh.py) instead of once. Each run is an independent sample;
re-running is always safe.
"""

import argparse
import time

from export import attrs, post
from jenkins import Jenkins

QUEUE_TREE = 'items[why,inQueueSince,task[name]]'
COMPUTER_TREE = 'computer[displayName,offline,idle,numExecutors]'


def snapshot(server):
    jenkins = Jenkins(server)
    now_ms = time.time() * 1000
    points = []  # (name, value, labels)

    queue = (jenkins.json(f'/queue/api/json?tree={QUEUE_TREE}') or {}).get('items', [])
    points.append(('ci_queue_depth', len(queue), {}))
    if queue:
        waits = [(now_ms - item['inQueueSince']) / 1000 for item in queue]
        points.append(('ci_queue_oldest_wait_seconds', max(waits), {}))
        for item, wait in zip(queue, waits):
            points.append(('ci_queue_item_wait_seconds', wait, {
                'ci_task': item['task']['name'][:200], 'ci_blocked': bool(item.get('why'))}))

    computer = (jenkins.json(f'/computer/api/json?tree={COMPUTER_TREE}') or {}).get('computer', [])
    agents = [c for c in computer if c.get('displayName')]
    for c in agents:
        labels = {'ci_agent': c['displayName']}
        points.append(('ci_agent_online', 0.0 if c.get('offline') else 1.0, labels))
        points.append(('ci_agent_idle', 1.0 if c.get('idle') else 0.0, labels))
        points.append(('ci_agent_executors', c.get('numExecutors') or 0, labels))
    points.append(('ci_agents_total', len(agents), {}))
    points.append(('ci_agents_offline', sum(1 for c in agents if c.get('offline')), {}))
    return points


def otlp_metrics(server, points):
    now_ns = str(int(time.time() * 1e9))
    by_name = {}
    for name, value, labels in points:
        by_name.setdefault(name, []).append({
            'timeUnixNano': now_ns, 'asDouble': float(value),
            'attributes': attrs({'ci_server': server, **labels})})
    resource = {'attributes': attrs({'service.name': f'jenkins-{server}', 'ci.server': server})}
    return {'resourceMetrics': [{'resource': resource, 'scopeMetrics': [{'metrics': [
        {'name': n, 'gauge': {'dataPoints': pts}} for n, pts in by_name.items()]}]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--server', choices=['ci', 'build'], required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    points = snapshot(args.server)
    agents = {l['ci_agent'] for n, v, l in points if n == 'ci_agent_online'}
    print(f'{args.server}: queue depth {next(v for n, v, l in points if n == "ci_queue_depth")}, '
          f'{len(agents)} agent(s), {len(points)} metric points')
    if not args.dry_run:
        post('/v1/metrics', otlp_metrics(args.server, points))


if __name__ == '__main__':
    main()
