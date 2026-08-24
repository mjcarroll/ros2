"""Compute the command environment from .dsv descriptors instead of spawning a shell.

colcon's default behaviour is to generate a shell script that dot-sources every
dependency's ``package.<ext>`` (plus each of its hooks) and then to spawn
PowerShell/cmd/sh to dump the resulting environment.  On Windows that costs
roughly ``438 + 60.5 * n_deps`` milliseconds per package.

Every hook that colcon generates is also emitted as a declarative ``.dsv``
descriptor, so the same environment can be derived in-process with no shell at
all.  This extension does that, and falls back to the shell-based extensions
whenever it encounters something it cannot evaluate faithfully.
"""

import os
import sys
from pathlib import Path

from colcon_core.logging import colcon_logger
from colcon_core.plugin_system import satisfies_version
from colcon_core.plugin_system import SkipExtensionException
from colcon_core.shell import check_dependency_availability
from colcon_core.shell import ShellExtensionPoint

logger = colcon_logger.getChild(__name__)

"""Set this to a non-empty value to fall back to the shell-based extensions."""
DISABLE_ENVIRONMENT_VARIABLE = 'COLCON_DSV_ENV_DISABLE'

DSV_TYPE_APPEND_NON_DUPLICATE = 'append-non-duplicate'
DSV_TYPE_PREPEND_NON_DUPLICATE = 'prepend-non-duplicate'
DSV_TYPE_PREPEND_NON_DUPLICATE_IF_EXISTS = 'prepend-non-duplicate-if-exists'
DSV_TYPE_SET = 'set'
DSV_TYPE_SET_IF_UNSET = 'set-if-unset'
DSV_TYPE_SOURCE = 'source'

"""Script extensions that could actually be sourced on this platform.

colcon's own descriptor walker only considers the primary and additional
extension of the shell in use and silently ignores every other one (``.txt``
markers, ``.bash``/``.zsh`` completion scripts on Windows, ...).  Mirroring that
matters: treating an irrelevant extension as "a script we must run" would send
almost every package down the shell fallback.
"""
SCRIPT_EXTENSIONS = ('ps1', 'bat') if sys.platform == 'win32' else ('sh',)


class NeedsShell(Exception):
    """Raised when a descriptor references a script we cannot evaluate."""


# parsed .dsv files keyed by (path, mtime_ns, size) so an edited file is re-read
_dsv_cache = {}

# the descriptor tree is walked once per dependent package -- tens of thousands
# of times for a full workspace -- and is dominated by repeated stat() calls on
# the same handful of paths, so memoize existence for the life of the process
_exists_cache = {}


def _exists(path):
    result = _exists_cache.get(path)
    if result is None:
        result = os.path.exists(path)
        _exists_cache[path] = result
    return result


def _parse_dsv(dsv_path):
    """Parse a .dsv file into a list of (type, remainder) tuples, cached."""
    try:
        st = dsv_path.stat()
    except OSError:
        return []
    key = (str(dsv_path), st.st_mtime_ns, st.st_size)
    cached = _dsv_cache.get(key)
    if cached is not None:
        return cached

    entries = []
    with dsv_path.open('r') as h:
        for i, line in enumerate(h.read().splitlines()):
            if not line.strip() or line.startswith('#'):
                continue
            try:
                type_, remainder = line.split(';', 1)
            except ValueError:
                raise RuntimeError(
                    "Line %d in '%s' doesn't contain a semicolon separating "
                    'the type from the arguments' % (i + 1, dsv_path))
            entries.append((type_, remainder))
    _dsv_cache[key] = entries
    return entries


class _Evaluator:
    """Apply .dsv descriptors to an environment dictionary."""

    def __init__(self, env):
        self.env = env

    def _set(self, name, value):
        self.env[name] = value

    def _set_if_unset(self, name, value):
        if not self.env.get(name):
            self.env[name] = value

    def _prepend_unique(self, name, value):
        current = self.env.get(name)
        if current:
            if value in current.split(os.pathsep):
                return
            self.env[name] = value + os.pathsep + current
        else:
            self.env[name] = value

    def _append_unique(self, name, value):
        current = self.env.get(name)
        if current:
            if value in current.split(os.pathsep):
                return
            self.env[name] = current + os.pathsep + value
        else:
            self.env[name] = value

    def _handle_except_source(self, type_, remainder, prefix):
        if type_ in (DSV_TYPE_SET, DSV_TYPE_SET_IF_UNSET):
            try:
                env_name, value = remainder.split(';', 1)
            except ValueError:
                raise RuntimeError(
                    "doesn't contain a semicolon separating the environment "
                    'name from the value')
            try_prefixed = os.path.join(prefix, value) if value else prefix
            if _exists(try_prefixed):
                value = try_prefixed
            if type_ == DSV_TYPE_SET:
                self._set(env_name, value)
            else:
                self._set_if_unset(env_name, value)
        elif type_ in (
            DSV_TYPE_APPEND_NON_DUPLICATE,
            DSV_TYPE_PREPEND_NON_DUPLICATE,
            DSV_TYPE_PREPEND_NON_DUPLICATE_IF_EXISTS,
        ):
            parts = remainder.split(';')
            env_name, values = parts[0], parts[1:]
            for value in values:
                if not value:
                    value = prefix
                elif not os.path.isabs(value):
                    value = os.path.join(prefix, value)
                if type_ == DSV_TYPE_PREPEND_NON_DUPLICATE_IF_EXISTS:
                    if not _exists(value):
                        continue
                    self._prepend_unique(env_name, value)
                elif type_ == DSV_TYPE_APPEND_NON_DUPLICATE:
                    self._append_unique(env_name, value)
                else:
                    self._prepend_unique(env_name, value)
        else:
            raise RuntimeError(
                'contains an unknown environment hook type: ' + type_)

    def process_dsv_file(self, dsv_path, prefix):
        """Evaluate one .dsv file, recursing into sourced descriptors."""
        for type_, remainder in _flatten(dsv_path, prefix):
            try:
                self._handle_except_source(type_, remainder, prefix)
            except RuntimeError as e:
                raise RuntimeError("In '%s' %s" % (dsv_path, e)) from e


# a package's descriptor tree is walked once per dependent package; flattening
# it to a linear op list once turns tens of thousands of tree walks into a
# dictionary lookup plus a replay
_flat_cache = {}


def _flatten(dsv_path, prefix):
    """Resolve a .dsv file and everything it sources into a flat op list."""
    key = (str(dsv_path), prefix)
    cached = _flat_cache.get(key)
    if cached is not None:
        return cached

    ops = []
    basenames = {}
    for type_, remainder in _parse_dsv(dsv_path):
        if type_ != DSV_TYPE_SOURCE:
            ops.append((type_, remainder))
            continue
        path_without_ext, ext = os.path.splitext(remainder)
        ext = ext[1:] if ext.startswith('.') else ext
        # mirror colcon: extensions we do not know about are ignored
        if ext not in SCRIPT_EXTENSIONS:
            continue
        basenames.setdefault(path_without_ext, set()).add(ext)

    for basename, extensions in basenames.items():
        if not os.path.isabs(basename):
            basename = os.path.join(prefix, basename)
        sibling = basename + '.dsv'
        if _exists(sibling):
            ops += _flatten(Path(sibling), prefix)
        else:
            # a real script with no declarative equivalent -- we cannot
            # reproduce its effect without running a shell
            raise NeedsShell(
                '%s has no .dsv equivalent'
                % (basename + '.' + sorted(extensions)[0]))

    _flat_cache[key] = ops
    return ops


class DsvEnvShell(ShellExtensionPoint):
    """Derive the command environment from .dsv files without a shell."""

    # above colcon_powershell (300), colcon_core sh/bat (200); below the
    # ros_domain_id coordinator (900) which only mutates os.environ and skips
    PRIORITY = 400

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(ShellExtensionPoint.EXTENSION_POINT_VERSION, '^2.2')

    # This extension only computes the command environment; it creates no
    # scripts of its own.  colcon still offers every extension above the base
    # priority a chance to create prefix/package/hook artifacts and logs an
    # ERROR for anything that raises, so opt out explicitly -- the same way
    # colcon_ros_domain_id_coordinator does, including the inert sentinel path
    # (an extension colcon's descriptor walker ignores).
    SENTINEL = Path('colcon_dsv_env.txt')

    def get_file_extensions(self):  # noqa: D102
        return ()

    def create_prefix_script(self, prefix_path, merge_install):  # noqa: D102
        return []

    def create_package_script(  # noqa: D102
        self, prefix_path, pkg_name, hooks,
    ):
        return []

    def create_hook_set_value(  # noqa: D102
        self, env_hook_name, prefix_path, pkg_name, name, value,
    ):
        return self.SENTINEL

    def create_hook_append_value(  # noqa: D102
        self, env_hook_name, prefix_path, pkg_name, name, subdirectory,
    ):
        return self.SENTINEL

    def create_hook_prepend_value(  # noqa: D102
        self, env_hook_name, prefix_path, pkg_name, name, subdirectory,
    ):
        return self.SENTINEL

    def create_hook_include_file(  # noqa: D102
        self, env_hook_name, prefix_path, pkg_name, relative_path,
    ):
        return self.SENTINEL

    async def generate_command_environment(  # noqa: D102
        self, task_name, build_base, dependencies,
    ):
        if os.environ.get(DISABLE_ENVIRONMENT_VARIABLE):
            raise SkipExtensionException(
                'disabled via ' + DISABLE_ENVIRONMENT_VARIABLE)

        # same bookkeeping the shell extensions do: prune dependencies which
        # are already provided by the environment, error on genuinely missing
        check_dependency_availability(
            dependencies, script_filename='package.dsv')

        env = dict(os.environ)
        evaluator = _Evaluator(env)
        try:
            for pkg_name, pkg_install_base in dependencies.items():
                prefix = str(pkg_install_base)
                dsv = Path(prefix) / 'share' / pkg_name / 'package.dsv'
                evaluator.process_dsv_file(dsv, prefix)
        except NeedsShell as e:
            raise SkipExtensionException(
                'cannot evaluate descriptors in Python: %s' % e)

        env.pop('COLCON_CURRENT_PREFIX', None)

        # mirror the shell extensions' debugging artifact
        env_path = Path(build_base) / (
            'colcon_command_prefix_%s.dsv.env' % task_name)
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            with env_path.open('w') as h:
                for key in sorted(env):
                    h.write('{}={}\n'.format(key, env[key]))
        except OSError as e:
            logger.debug('could not write %s: %s' % (env_path, e))

        return env
