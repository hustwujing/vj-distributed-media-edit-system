import unittest
from unittest.mock import patch
from webapp.native_network import rules,verify


class NativeNetworkTests(unittest.TestCase):
    def setUp(self):
        self.network={'Name':'sandbox-agent-net','Id':'a'*64,'Internal':True,
            'IPAM':{'Config':[{'Subnet':'172.31.0.0/24','Gateway':'172.31.0.1'}]}}

    def test_only_dedicated_private_network_may_be_changed(self):
        for key,value in [('Name','bridge'),('Internal',False),('EnableIPv6',True),('Id','invalid')]:
            with self.subTest(key=key),self.assertRaises(ValueError):rules({**self.network,key:value})
        selected=rules(self.network)
        self.assertEqual(len(selected),3)
        self.assertTrue(all('-i' in rule and 'br-aaaaaaaaaaaa' in rule for _,rule in selected))
        self.assertEqual(selected[-1][1][-1],'DROP')
        self.assertIn('18880:18949',selected[1][1])

    def test_missing_guard_refuses_allocation_without_installing_any_rule(self):
        import subprocess
        with patch('webapp.native_network.subprocess.check_output',return_value=b'[{"ifname":"br-test","addr_info":[{"local":"172.31.0.1"}]}]'), \
             patch('webapp.native_network.subprocess.run',side_effect=subprocess.CalledProcessError(1,'iptables')) as run:
            with self.assertRaises(subprocess.CalledProcessError):verify('172.31.0.1','172.31.0.0/24')
            self.assertIn('-C',run.call_args.args[0]);self.assertNotIn('-I',run.call_args.args[0])
