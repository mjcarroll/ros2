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
2. build.ros2.org: decided to track infra/queue/agent health only, not the 66k per-package jobs (not
   worth the crawl; see below).
3. Correlate with GitHub commits; hang/regression alerts.
4. Hosting and retention conversation with whoever runs the infra, if it should outlive a laptop.

## build.ros2.org: infra/queue/agent health only (decided)

Not the 66k per-package jobs: never worth crawling per the scale note above, and no per-package
dashboard was asked for. Instead:

- `buildfarm.py --server build` (and `ci`, for the same reason ci.ros2.org's queue wasn't tracked
  either): polls `/queue/api/json` and `/computer/api/json` and pushes point-in-time gauges
  (`ci_queue_depth`, `ci_queue_oldest_wait_seconds`, `ci_queue_item_wait_seconds` by task,
  `ci_agent_online`/`ci_agent_idle`/`ci_agent_executors`, `ci_agents_total`, `ci_agents_offline`).
  No cache, no backfill: Jenkins keeps no queue history, so this only has meaning run on an interval.
- `check_agents` / `ci__infra_*`: ordinary jobs, so they go through the existing fetch.py/export.py
  pipeline unchanged (`fetch.py --server build --job check_agents --match ci__infra_`). They show up
  in the existing per-job dashboards ("ROS 2 CI overview", "CI health") once `$job` is set to one of
  them; the exact names are a guess from convention and need confirming against a real token
  (see Open questions).
- New dashboard `ros2-ci-buildfarm` ("Build farm health"): queue depth/wait and agent status, with a
  `$server` (ci/build) picker instead of `$job`, since this is server-level, not per-job.
- `ci_build_result`/`ci_build_duration_seconds`/etc. now carry a `ci_server` label (previously only
  traces/logs had it via `service.name`). The "All nightlies" matrix dashboard is scoped to
  `ci_server="ci"` so build.ros2.org's infra jobs don't show up mixed into a scorecard titled
  "nightlies".
- `refresh.py` now also runs the build-server fetch/export and both buildfarm snapshots; exit code 0
  requires all of them to succeed, same as the ci.ros2.org steps.

## Open questions

- Does the token authenticate against the GitHub OAuth setup with Basic auth on both servers? (untested)
- How far back do builds and Timestamper data go? Jenkins keeps about 100 builds per job (test history showed ~85-100).
- Is Timestamper enabled on every job, and on build.ros2.org?
- What does the JavaMelody `/monitoring` endpoint expose to a user token?
- ~~Which build.ros2.org jobs are worth tracking (per distro/package vs infra only)?~~ Decided:
  infra/queue/agent only (see above).
- ~~Are `check_agents` and the `ci__infra_*` prefix the real job names on build.ros2.org?~~ Confirmed
  2026-10-21 with a token: `check_agents`, `ci__infra_ubuntu_jammy_amd64`,
  `ci__infra_ubuntu_noble_amd64`, `ci__infra_ubuntu_resolute_amd64` — exactly 4 jobs, no false
  positives from the `ci__infra_` prefix against all 66,026 build.ros2.org job names.
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

### More gotchas (2026-10-21)

- build.ros2.org can return an HTML "are you a bot" challenge page with HTTP 200 for `/testReport/api/json`
  on a job with no test report, instead of Jenkins' usual 404. `export.py`'s loader now treats an
  undecodable cached test report as "no report" rather than failing the whole build (seen on `check_agents`).
- Only `ERRORISH` lines were ever sent to Loki, so a span for a package/test that passed cleanly had zero
  log lines — not a bug exactly, but a real gap against the original plan ("logs... start with
  severity-tagged lines... Starting/Finished"). `export.py` now logs every console line (INFO, or ERROR on
  an `ERRORISH` match or non-`Finished` outcome), tagged with the same package/test/section context used
  for spans, via `span_for`'s existing fallback (extended to also check top-level section spans, not just
  subsections). Volume is real: a ~5-6h aborted `nightly_win_rel` build is ~245-280k log lines. At that
  volume the Collector's `otlphttp/loki` exporter's default queue concurrency (>1 consumer) raced a handful
  of batches out of order into the same stream ("entry too far behind"), even though our client sends
  strictly sorted and sequential; fixed with `sending_queue.num_consumers: 1` on that exporter in
  `otel-collector.yaml`.
- The job-level staleness check (skip export if the *newest* cached build is older than `--max-age-days`)
  missed jobs with a sparse mix of ages: `test_packaging_linux` etc. had a couple of recent builds plus a
  few builds **884-1727 days old** sitting in the same cache dir (these are rarely-triggered validation
  jobs, so build numbers are sparse). The newest build looked fresh, so the job wasn't skipped, but the
  ancient builds' metric samples were older than Prometheus's `out_of_order_time_window` (8760h = 1 year)
  and got rejected ("too old sample"), failing verification for the whole job even though the fresh builds
  exported fine. Added a **per-build** age check (same flags) alongside the existing per-job one.
- `ci_launcher` (and similar dispatcher jobs) triggers downstream builds via Jenkins' own
  "trigger other projects" mechanism, which Jenkins records only on the *downstream* build as a
  `Cause$UpstreamCause` (`upstreamProject`/`upstreamBuild`) — not on the launcher's own build. `fetch.py`'s
  `BUILD_TREE` now asks for `causes[upstreamProject,upstreamBuild]`; `export.py` turns that into an OTLP
  span link from the downstream build's root span back to the launcher build's root span (same
  deterministic trace/span id scheme), plus `ci.upstream_job`/`ci.upstream_build` attributes and a
  `ci_triggered_by` metric (for a "what did build #N trigger" table). Only takes effect for builds fetched
  after this change — existing cached builds lack the `causes` field until re-fetched.
- Grafana's Tempo "Logs for this span" (`tracesToLogsV2.filterBySpanID`) silently returned nothing: Loki only
  indexes `service_namespace`/`service_instance_id` as real labels (confirmed via `/loki/api/v1/labels`);
  `trace_id`/`span_id` are structured metadata, and whatever field name Grafana's span-id filter stage adds
  on top didn't match. Turned it off (`filterBySpanID: false`); the tag-based selector alone (proven working
  by the "Builds" log panel) is what "Logs for this span" now falls back to — effectively "logs for this
  build", which matches our per-build (not per-span) Loki stream granularity anyway.
