#!/usr/bin/env python3
"""Pinned staging-only egress policy. Never fetched/executed by app updates."""
import argparse,json,os,socket,subprocess
NETWORK='mlai-staging-api-internal'
SUBNET='172.28.240.0/24'
BRIDGE='mlai-stage-api'
CHAIN='MLAI_STAGE_API'
HOST_CHAIN='MLAI_STAGE_HOST'
SOURCE='172.28.240.10-172.28.240.19'

def run(args,check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True,timeout=30)
def inspect(kind,name):return json.loads(run(['docker',kind,'inspect',name]).stdout)[0]
def validate_network(n):
    if n.get('Name')!=NETWORK or n.get('Internal') is not True or n.get('EnableIPv6') or n.get('IPAM',{}).get('Config')!=[{'Subnet':SUBNET}]:
        # Docker also includes a Gateway after creation.
        configs=n.get('IPAM',{}).get('Config',[])
        if not (n.get('Name')==NETWORK and n.get('Internal') is True and not n.get('EnableIPv6') and len(configs)==1 and configs[0].get('Subnet')==SUBNET and set(configs[0])<={'Subnet','Gateway'}):
            raise ValueError('Unexpected internal network identity/topology.')
    if n.get('Options',{}).get('com.docker.network.bridge.name',BRIDGE)!=BRIDGE:raise ValueError('Unexpected bridge identity.')

def validate_api(c):
    networks=c.get('NetworkSettings',{}).get('Networks',{})
    h=c.get('HostConfig',{})
    if (set(networks)!={NETWORK} or c.get('Config',{}).get('Labels',{}).get('au.mlai.environment')!='chat-staging-api'
        or h.get('Privileged') or h.get('CapAdd') or h.get('NetworkMode') in {'host','none'}
        or any(port for port in c.get('NetworkSettings',{}).get('Ports',{}).values())
        or any(m.get('Source') in {'/var/run/docker.sock','/run/docker.sock','/'} for m in c.get('Mounts',[]))):
        raise ValueError('API identity, mounts, privilege or attachment changed.')
    n=networks[NETWORK];ip=n.get('IPAddress','')
    if ip not in [f'172.28.240.{i}' for i in range(10,20)] or n.get('GlobalIPv6Address'):
        raise ValueError('API is outside protected IPv4 range or has IPv6.')

def rules():
    return [['-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','RETURN'],
        *[['-d',destination,'-p','tcp','--dport',port,'-j','RETURN'] for destination,port in [('172.28.240.2','5432'),('172.28.240.3','6379'),('172.28.240.4','3100')]],['-j','DROP']]
def ipt(*args,check=True):return run(['iptables','-w','10',*args],check)
def ensure_rule(chain,rule,position=False):
    if ipt('-C',chain,*rule,check=False).returncode:
        ipt('-I' if position else '-A',chain,*(['1'] if position else []),*rule)
def anchors():
    source=['-i',BRIDGE,'-m','iprange','--src-range',SOURCE]
    return [('DOCKER-USER',source+['-j',CHAIN]),('INPUT',source+['-j',HOST_CHAIN])]
def verify_rules():
    for chain,rule in anchors():ipt('-C',chain,*rule)
    for rule in rules():ipt('-C',CHAIN,*rule)
    ipt('-C',HOST_CHAIN,'-j','DROP')
    # No unexpected ACCEPT/RETURN can precede installed rules.
    for chain,expected in [(CHAIN,rules()),(HOST_CHAIN,[['-j','DROP']])]:
        actual=[line for line in ipt('-S',chain).stdout.splitlines() if line.startswith('-A ')]
        if len(actual)!=len(expected):raise ValueError('Owned chain contains unexpected rules.')
def topology():
    validate_network(inspect('network',NETWORK))
    ids=run(['docker','ps','-q','--filter','label=au.mlai.environment=chat-staging-api']).stdout.split()
    for identifier in ids:validate_api(inspect('container',identifier))
    # Fixed dependency destinations must belong to the exact staging services.
    expected={'postgres-api':'172.28.240.2','redis-api':'172.28.240.3','membership-adapter':'172.28.240.4','caddy':'172.28.240.5'}
    n=inspect('network',NETWORK)
    found={}
    for identifier in n.get('Containers',{}):
        c=inspect('container',identifier);labels=c.get('Config',{}).get('Labels',{});service=labels.get('com.docker.compose.service');project=labels.get('com.docker.compose.project')
        ip=c.get('NetworkSettings',{}).get('Networks',{}).get(NETWORK,{}).get('IPAddress')
        if service in expected:
            wanted='mlai-chat-staging-api' if service in {'postgres-api','redis-api'} else 'mlai-chat-staging'
            if project!=wanted or ip!=expected[service] or service in found:raise ValueError('Dependency identity mismatch.')
            found[service]=ip
    if set(found)!=set(expected):raise ValueError('Missing isolated dependency or edge attachment.')
def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['install','verify','remove-owned']);a=p.parse_args()
    if os.geteuid()!=0 or socket.gethostname()!='ubuntu-s-4vcpu-8gb-syd1':raise SystemExit('Only the approved staging host may install this policy.')
    if a.action=='install':
        validate_network(inspect('network',NETWORK))
        for chain in (CHAIN,HOST_CHAIN):
            ipt('-N',chain,check=False)
        # Never flush any chain. Refuse drift instead of altering other rules.
        for rule in rules():ensure_rule(CHAIN,rule)
        ensure_rule(HOST_CHAIN,['-j','DROP'])
        for chain,rule in anchors():ensure_rule(chain,rule,position=True)
        verify_rules()
    elif a.action=='verify':topology();verify_rules()
    else:
        if run(['docker','ps','-q','--filter','label=au.mlai.environment=chat-staging-api']).stdout.strip():raise SystemExit('Stop APIs before removing owned policy.')
        for chain,rule in anchors():
            if ipt('-C',chain,*rule,check=False).returncode==0:ipt('-D',chain,*rule)
        for chain in (CHAIN,HOST_CHAIN):
            ipt('-F',chain);ipt('-X',chain)
    print('Staging network policy '+a.action+' complete.')
if __name__=='__main__':main()
