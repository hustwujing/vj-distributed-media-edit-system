import unittest

from webapp.opensandbox_runtime import command_factory_from_environment
from webapp.sandbox_backends import BackendConfigurationError


class OpenSandboxRuntimeWiringTests(unittest.TestCase):
    def test_disabled_command_backend_builds_no_factory(self):
        self.assertIsNone(command_factory_from_environment({}))

    def test_enabled_backend_requires_connection_and_confirmed_internal_bridge(self):
        with self.assertRaisesRegex(BackendConfigurationError,'URL'):
            command_factory_from_environment({'VIDEO_USE_CPU_COMMAND_SANDBOX_BACKEND':'opensandbox'})
        values={'VIDEO_USE_CPU_COMMAND_SANDBOX_BACKEND':'opensandbox',
            'VIDEO_USE_OPEN_SANDBOX_URL':'http://127.0.0.1:28090',
            'VIDEO_USE_OPEN_SANDBOX_API_KEY':'secret',
            'VIDEO_USE_OPEN_SANDBOX_NETWORK_ISOLATION':'internal_bridge'}
        with self.assertRaisesRegex(BackendConfigurationError,'confirmed'):
            command_factory_from_environment(values)
        factory=command_factory_from_environment({**values,
            'VIDEO_USE_OPEN_SANDBOX_INTERNAL_BRIDGE_CONFIRMED':'1'})
        self.assertEqual(factory.backend.lifecycle.network_policy,'internal_bridge')


if __name__=='__main__':unittest.main()
