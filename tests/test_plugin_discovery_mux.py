"""Exercise real Hermes discovery and adapter construction, without connecting a gateway.

Run with the Hermes runtime Python and PYTHONPATH pointing to the Hermes checkout:
  python -m unittest tests.test_plugin_discovery_mux
Each case uses a fresh subprocess and disposable host and secondary profile.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]

CHILD = r'''
import os, pathlib, shutil
from types import SimpleNamespace
home = pathlib.Path(os.environ['HERMES_HOME'])
profile = home / 'profiles' / 'family'
target = profile / 'plugins' / 'platforms' / 'bluebubbles'
target.parent.mkdir(parents=True)
shutil.copytree(os.environ['PLUGIN_SOURCE'], target, ignore=shutil.ignore_patterns('__pycache__'))
(profile / 'config.yaml').write_text(os.environ['PROFILE_CONFIG'])
from hermes_cli.plugins import get_plugin_manager
from gateway.run import _profile_runtime_scope
from gateway.platform_registry import platform_registry
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.config import Platform, PlatformConfig
# Reproduce a shared host that discovers its bundled deferred copy first.
host = get_plugin_manager()
host.discover_and_load()
assert 'bluebubbles' in platform_registry.pending_names()
with _profile_runtime_scope(profile, hydrate_secrets=False):
    manager = get_plugin_manager()
    manager.discover_and_load()
    record = manager._plugins['platforms/bluebubbles']
    from tools.registry import registry
    if os.environ['EXPECT_ENABLED'] == 'yes':
        assert registry.get_entry('bluebubbles_tapback') is not None
        assert 'bluebubbles_tapback' in record.tools_registered
        from toolsets import resolve_toolset
        assert 'bluebubbles_tapback' in resolve_toolset('bluebubbles')
    assert pathlib.Path(record.manifest.path) == target
    runner = SimpleNamespace(config=SimpleNamespace(group_sessions_per_user=False),
        _instantiate_adapter=lambda p,c: GatewayAdapterLifecycleMixin._instantiate_adapter(runner,p,c))
    cfg = PlatformConfig(enabled=True, extra={'server_url':'http://example.invalid','password':'fake'})
    adapter = GatewayAdapterLifecycleMixin._create_adapter(runner, Platform.BLUEBUBBLES, cfg)
    if os.environ['EXPECT_ENABLED'] == 'yes':
        assert record.enabled and not record.deferred and record.error is None, record
        assert adapter is not None
        assert pathlib.Path(sys.modules[adapter.__class__.__module__].__file__).parent == target
        assert adapter.gateway_runner is runner
        # Exercise Hermes' actual deferred pre-registration path with this same
        # user manifest in isolation, before materializing its adapter.
        manager.unload()
        manager._register_deferred_platform(record.manifest)
        deferred = manager._plugins['platforms/bluebubbles']
        assert deferred.deferred, deferred
        assert 'bluebubbles_tapback' in deferred.tools_registered
        assert registry.get_entry('bluebubbles_tapback') is not None
    else:
        assert not record.enabled and 'not enabled in config' in record.error, record
        assert adapter is None
'''

class DiscoveryMuxTest(unittest.TestCase):
    def run_case(self, enabled):
        with tempfile.TemporaryDirectory(prefix='bluebubbles-discovery-') as root:
            env = os.environ.copy()
            env.update(HERMES_HOME=root, HERMES_DISABLE_LAZY_INSTALLS='1',
                       HERMES_SUPERVISED_CHILD='1', PLUGIN_SOURCE=str(PROJECT / 'plugin'),
                       PROFILE_CONFIG=('plugins:\n  enabled:\n    - platforms/bluebubbles\n' if enabled
                                       else 'plugins:\n  enabled:\n    - unrelated-plugin\n'),
                       EXPECT_ENABLED='yes' if enabled else 'no')
            result = subprocess.run([sys.executable, '-c', 'import sys\n' + CHILD], env=env,
                                    text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shadow_without_explicit_enable_fails_closed(self):
        self.run_case(False)

    def test_enabled_override_constructs_in_secondary_scope(self):
        self.run_case(True)
