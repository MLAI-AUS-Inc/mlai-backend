import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('policy',Path(__file__).with_name('network-policy.py'))
class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.p)
        self.network={'Name':self.p.NETWORK,'Internal':True,'EnableIPv6':False,'IPAM':{'Config':[{'Subnet':self.p.SUBNET}]},'Options':{'com.docker.network.bridge.name':self.p.BRIDGE}}
        self.container={'Name':'/api','Config':{'Labels':{'au.mlai.environment':'chat-staging-api'}},'HostConfig':{'Privileged':False,'NetworkMode':self.p.NETWORK,'CapAdd':[],'CapDrop':['ALL']},'Mounts':[],'NetworkSettings':{'Networks':{self.p.NETWORK:{'IPAddress':'172.28.240.10','GlobalIPv6Address':''}},'Ports':{}}}
    def test_network_and_identity_fail_closed(self):
        self.p.validate_network(self.network)
        self.p.validate_api(self.container)
        for key,value in [('Internal',False),('EnableIPv6',True),('Name','other')]:
            changed={**self.network,key:value}
            with self.assertRaises(ValueError):self.p.validate_network(changed)
        self.container['NetworkSettings']['Networks']['chat']={'IPAddress':'172.19.0.4'}
        with self.assertRaises(ValueError):self.p.validate_api(self.container)
    def test_host_ipv6_mounts_and_privileges_fail_closed(self):
        for mutate in (lambda c:c['HostConfig'].update(Privileged=True),lambda c:c['HostConfig'].update(CapAdd=['NET_ADMIN']),lambda c:c['Mounts'].append({'Source':'/var/run/docker.sock'}),lambda c:c['NetworkSettings']['Networks'][self.p.NETWORK].update(GlobalIPv6Address='fd00::10')):
            import copy
            c=copy.deepcopy(self.container);mutate(c)
            with self.assertRaises(ValueError):self.p.validate_api(c)
    def test_rules_allow_only_dependencies_and_established_reply(self):
        rules=self.p.rules()
        self.assertTrue(any('ESTABLISHED,RELATED' in rule for rule in rules))
        for port in ('5432','6379','3100'):self.assertTrue(any(port in rule for rule in rules))
        self.assertEqual(rules[-1][-1],'DROP')
    def test_bridge_option_must_be_explicit(self):
        missing={**self.network,'Options':{}}
        with self.assertRaises(ValueError):self.p.validate_network(missing)
    def test_rules_and_parent_anchors_must_have_effective_order(self):
        from unittest.mock import patch
        import types
        def output(args):
            chain=args[1]
            if chain==self.p.CHAIN:rules=list(reversed(self.p.rules()))
            elif chain==self.p.HOST_CHAIN:rules=[['-j','DROP']]
            else:rules=[['-j','ACCEPT'],dict(self.p.anchors())[chain]]
            return '\n'.join('-A '+chain+' '+' '.join(r) for r in rules)
        with patch.object(self.p,'ipt',side_effect=lambda *args,**kwargs:types.SimpleNamespace(returncode=0,stdout=output(args) if args[0]=='-S' else '')):
            with self.assertRaises(ValueError):self.p.verify_rules()
