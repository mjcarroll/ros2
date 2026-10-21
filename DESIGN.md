# ROS 2 CI observability sketch

Status: sketch, nothing built. Facts marked (probed) were read from the live servers on 2026-10-19
through a signed-in user session; everything else is a plan or an assumption.

## Goal

Trace parts of a CI build and see timing effects over the long term: where a nightly spent its time,
which package or test is slow or hangs, which agents misbehave, how queue wait changes. Today this
means paging 25 MB consoles through a browser.

## What the servers expose

| | ci.ros2.org | build.ros2.org |
|---|---|---|
| Jobs | 48 (nightly_*, ci_*, packaging_*) (probed) | 66,003, mostly per-package `<distro>bin_<platform>__<pkg>__...` (probed) |
| Agents | 19, 1 executor each, none offline (probed) | 19 (probed) |
| Queue API | yes | yes (7 items when probed) |
| Per-agent monitors | swap, disk, architecture, response time, clock (probed) | not probed |
| Plugin list | readable (112 plugins) (probed) | 403, admin only (probed) |
| Timestamper | installed 1.30; `/timestamps/` works on nightly_win_rel #3946, #3959 (probed) | not probed |
| build-timeout | installed 1.41 (the 600 min "Build timed out") | not probed |
| JavaMelody `monitoring` | installed 2.8.0, endpoint not probed | not probed |
| OpenTelemetry / Prometheus / Metrics plugins | **not installed** (404s) (probed) | **not installed** (404s) (probed) |
| Archived artifacts | none on nightly_win_rel #3946 (probed) | not probed |

A user API token gives read access to everything in the first two rows plus the console. It does not
give plugins, config, or anything admin only. So the pipeline has to **pull and reconstruct**: nothing can
push live spans from Jenkins or from the build scripts without upstream changes.

## What a token lets us build

ci.ros2.org:
- Build records: result, start, duration, agent (`builtOn`), queue wait.
- Timeline per build from `/timestamps/` console: `Starting >>> pkg` / `Finished <<< pkg [t]` (build and test),
  ctest `Test #N: name ... Passed X sec` lines (end time = line time, start = end - duration),
  launch test starts, and idle gaps. A hang is the last timestamp before "Build timed out" with a package still open.
- Test cases: status, duration, age (builds failing), error text, truncated stdout. No per-case start time.
- Agent health: monitor data over time, correlated with failures (the Windows container multicast problem
  would show up as "fails on every agent", which is itself informative).
- Queue depth and wait time per job over time.
- Correlation with changes: commits per repo between consecutive nightlies via the GitHub API.

build.ros2.org (to be verified with a token):
- Per-package binary build results and durations by distro/platform, failure rates, queue backlog,
  agent utilisation, `check_agents` / `ci__infra_*` job health.
- Scale: 66k jobs. Never crawl per job. Use view or bulk `tree=` queries and the queue/computer APIs.

Not possible without upstream changes: archived colcon `events.log`, live propagation (`TRACEPARENT`), anything
from inside the agent (container network state, host resources), test start times.

## Architecture

```
Jenkins (ci / build) --HTTPS+token--> collector (Python, polite, incremental)
                                         |-- raw cache (gzip json + console) on disk
                                         `-- exporter --OTLP--> OTel Collector
                                                                  |-- Tempo (traces)
                                                                  |-- Loki (logs)
                                                                  `-- Prometheus/Mimir (metrics)  --> Grafana
```

Telemetry model:
- **Trace per build.** Trace id derived deterministically from `<server>/<job>/<number>`, so reruns do not duplicate.
  Root span = build. Children: queue wait, node, colcon build, colcon test, one span per package (build and test),
  one per ctest test. Attributes: job, number, agent, result, RMW/vendor flags, branch, package.
- **Logs**: timestamped console lines; start with severity-tagged lines (errors, test failures, timeouts,
  Starting/Finished) rather than all 250k lines, then widen if useful. Linked to the trace by id.
- **Metrics**: build duration, queue wait, package duration, test duration, failures, flake rate, executors busy.

## Collector behaviour

- One concurrent request, backoff on 429, honor `Retry-After`. nginx rate-limits after a few big fetches (seen).
- Fetch each console once, store gzipped, parse locally. Incremental by build number per job.
- Idempotent export; watermark per job.
- Read-only token per server, from env (`JENKINS_CI_USER`/`JENKINS_CI_TOKEN`, same for build). Never in the repo.

## Plan

1. Local stack (docker compose: Collector, Tempo, Loki, Prometheus, Grafana) plus collector for
   `nightly_win_rel`, `nightly_linux_release`, `ci_windows`. Dashboards: slowest packages, builds that timed
   out and what was open, failure rate by agent, queue wait.
2. build.ros2.org: queue, agents, bulk per-distro build results; decide which of the 66k jobs matter.
3. Correlate with GitHub commits; hang/regression alerts.
4. Hosting and retention conversation with whoever runs the infra, if it should outlive a laptop.

## Open questions

- Does the token authenticate against the GitHub OAuth setup with Basic auth on both servers? (untested)
- How far back do builds and Timestamper data go? Jenkins keeps about 100 builds per job (test history showed ~85-100).
- Is Timestamper enabled on every job, and on build.ros2.org?
- What does the JavaMelody `/monitoring` endpoint expose to a user token?
- Which build.ros2.org jobs are worth tracking (per distro/package vs infra only)?
- Where does this run long term?

## Smoke test results (2026-10-19, local stack, hand-made data via OTLP/HTTP on :4318)

- Traces: a trace 3 days old was stored and returned by id from Tempo (via Grafana proxy).
- Logs: a 3-day-old line was stored in Loki with trace/span ids as structured metadata
  (`reject_old_samples: false`).
- Metrics: a 3-day-old sample was accepted only as the first sample of a series. Writing an older sample into
  a series that already had newer ones failed with `out of bounds` until
  `storage.tsdb.out_of_order_time_window: 2160h` was set in `prometheus.yml`; then it was accepted.
  So backfill order and that window matter. Windows longer than the retained history need a different tool
  (promtool backfill, or VictoriaMetrics).
- **The Collector returns HTTP 200 before the backend write.** The rejected Prometheus write was reported only
  in the Collector log ("Exporting failed. Dropping data"). The exporter must check
  `otelcol_exporter_send_failed_*` (or the logs) after each batch, or data loss will be silent.

## Built so far (2026-10-19)

- `jenkins.py` / `fetch.py`: polite authenticated download into `cache/` (build.json, testReport, console with
  per-line UTC timestamps via `/timestamps/?time=yyyy-MM-dd HH:mm:ss.SSS&timeZone=UTC&appendLog`).
- `export.py`: parses the cached console into traces, logs and metrics, sends OTLP to the local stack, and
  verifies what the backends stored. Tracks exported builds in `cache/exported.json`.
- `make_dashboard.py`: generates `grafana/dashboards/ros2-ci-overview.json` (provisioned into Grafana).
- Console structure used: `"# BEGIN/END SECTION"` and `# BEGIN/END SUBSECTION` markers, `Starting >>>` /
  `Finished|Failed <<<` colcon lines (live, accurate), `--- output: <pkg>` blocks with ctest lines (flushed
  after the package finishes, so per-test times are estimates), and a `vcs export --exact` subsection that
  lists the exact commit of every repo (not yet used for correlation).
- Docker image build and the pixi/vcs setup are a fixed cost per nightly (about 16 min for the image in #3959).

### Backend gotchas found

- Loki: entries older than a stream's newest are rejected ("entry too far behind"). Fixed by sorting each
  build's logs and giving each build its own stream (`service.namespace` = job, `service.instance.id` = build).
  Re-exporting a build into an existing stream fails, so export once (`--force` only after wiping Loki).
- Tempo: time-range search returns nothing for backfilled traces (the same query with no range works), so
  dashboards link to traces by id instead of searching by time. Search also needs
  `query_frontend.search.max_duration: 0s` to span more than 7 days.
- Tempo span ids must be unique per trace: a package can run several ctest groups numbering tests from 1.
- Prometheus: needs `out_of_order_time_window`; sparse one-per-build samples need `last_over_time(...[$__interval])`.
- Resetting a volume needs `docker compose rm -sf <service>` first; `docker volume rm` fails while a stopped
  container still references it.

### What the first three builds showed

- #3959: `launch_testing_ros` (test phase) unfinished at the timeout: 5.92 h.
- #3951: `test_launch_ros` failed (the access-violation crash).
- #3946: slowest build packages are rviz_default_plugins, fastdds, zenoh_cpp_vendor, rclcpp.

### More gotchas (2026-10-19)

- Never run two exports at once: interleaved Loki writes lose entries. `export.py` now takes a lock
  (`cache/.export.lock`; delete it if a run crashed).
- Loki collapses identical lines (same timestamp and text) within a stream. colcon flushes a package's output as
  one block, so repeated identical messages in a block collapse to one entry: log counts per build are lower
  than the console, not lost.
- Trace size: spans are only created for slow (>= 60 s) or unfinished/failed packages and for failed/timed-out or
  slow (>= 30 s) tests; the rest are folded into counts on the phase/package span (`--pkg-min-seconds`,
  `--test-min-seconds`). Metrics still cover every package.
- `--metrics-only`, `--traces-only` and `--logs-only` re-send one signal after wiping that backend's volume.
