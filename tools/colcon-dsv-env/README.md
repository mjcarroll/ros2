# colcon-dsv-env

Derives colcon's per-package **command environment** from the `.dsv` descriptors
already present in the install tree, instead of spawning a shell.

## The problem

For every package it builds, colcon generates a script that dot-sources each
dependency's `package.<ext>` plus all of that package's hooks, then spawns
PowerShell/cmd/sh to dump the resulting environment. Measured on this workspace
(Windows, 22-core box, serial, idle):

```
env_setup_ms = 438 + 60.5 * n_deps
```

The median package has 93 dependencies, so ~6 s elapses before the first
compiler runs. Across 269 packages that is ~23,700 `package.ps1` and ~169,000
hook dot-sources — roughly **198,000 script executions and ~26 minutes** of pure
environment computation per build.

Almost all of it is redundant. With `merge-install` everything lands in one
prefix, so the result is nearly constant: across all 275 generated `.env` files
there are only 4 distinct `CMAKE_PREFIX_PATH` values and 8 distinct `PATH`
values. For `rcutils`, only 5 of its 53 dependency scripts changed any variable.

Switching shells does not help — cmd/bat measured 3,698 ms against PowerShell's
3,478 ms for the same 53 dependencies.

## The approach

Every hook colcon generates is also emitted as a declarative `.dsv` descriptor,
and colcon already ships a pure-Python evaluator for them
(`_local_setup_util_<shell>.py`) — it just isn't wired into the build. This
extension registers a shell extension at `PRIORITY = 400` (above
`colcon-powershell`'s 300) that implements only `generate_command_environment`,
walking the descriptors in-process.

Descriptor trees are flattened and memoized, so the tens of thousands of
repeated walks collapse to a dictionary lookup plus a replay.

If a descriptor sources a real script with no `.dsv` equivalent, the extension
raises `SkipExtensionException` and colcon falls back to the shell extensions.

## Measured results

End-to-end, no-op rebuild, MSVC environment, `rc=0` both ways:

| package | deps | env setup (shell) | env setup (dsv) | total wall |
|---|---|---|---|---|
| `rcutils` | 53 | 3.61 s | 0.09 s | 7.8 s → 4.5 s |
| `ros2cli_common_extensions` | 172 | 16.05 s | 0.42 s | 23.4 s → 7.1 s |

Offline over all 269 packages (23,670 dependency evaluations): **1.07 s** total,
against ~26 min for the shell path.

## Correctness

Compared against the environment colcon's PowerShell path actually produced for
all 269 packages:

- **0 values that colcon produced are missing** — no regressions.
- 76 packages: byte-identical.
- 183 packages: same set of paths, different order (see below).
- 10 packages: this extension adds `<prefix>\bin`, which the PowerShell path
  **drops**.

That last group is a pre-existing colcon/ament bug, not a change in behaviour
here. Only 7 of 369 packages have a `local_setup.ps1`, yet every generated
`package.ps1` unconditionally sources one and emits a `Write-Error` when it is
missing — so ament's `environment/*.bat` hooks never get applied under
PowerShell. The `.bat` path *does* apply them, and this extension agrees with
`.bat` exactly. Verified directly:

```
ament_cmake_export_definitions (2 deps)
   cmd/bat shell  : W:\install_v\bin
   powershell     : <none>
```

The ordering differences in the other 183 follow from the same cause: applying
those hooks earlier changes where deduplicated entries land in `PATH`. The path
sets are identical.

## Usage

```bash
pixi run python -m pip install -e tools/colcon-dsv-env --no-deps --no-build-isolation
```

Set `COLCON_DSV_ENV_DISABLE=1` to fall back to the stock shell path without
uninstalling — this is what the A/B measurements above toggle.

To remove entirely:

```bash
pixi run python -m pip uninstall -y colcon-dsv-env
```

## Prototype caveats

- Like `colcon-ros-domain-id-coordinator`, this extension returns a sentinel
  path from its `create_hook_*` methods, so `source;colcon_dsv_env.txt` appears
  in the descriptors of packages built while it is installed. The line is inert
  (ignored by colcon's walker and by this extension), but it is a change to
  generated artifacts. A real upstream fix would instead add
  `generate_command_environment` to the existing `DsvShell` — which already
  creates genuine artifacts — and raise its priority above the shell extensions,
  avoiding the sentinel entirely.
- Only exercised on Windows against this workspace.
