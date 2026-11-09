"""Guard the dedicated sandbox bridge without changing other host networks."""
import argparse
import ipaddress
import json
import re
import subprocess


def rules(network):
    if network.get('Name')!='sandbox-agent-net' or not network.get('Internal') or network.get('EnableIPv6'):
        raise ValueError('Only the isolated IPv4 sandbox-agent-net is supported')
    identifier=network.get('Id','')
    if not re.fullmatch(r'[0-9a-f]{64}',identifier):raise ValueError('Invalid bridge identity')
    configurations=network.get('IPAM',{}).get('Config',[])
    if len(configurations)!=1:raise ValueError('Exactly one private bridge subnet is required')
    subnet=ipaddress.ip_network(configurations[0]['Subnet'],strict=True)
    gateway=ipaddress.ip_address(configurations[0]['Gateway'])
    if not subnet.is_private or gateway not in subnet:raise ValueError('Invalid private sandbox bridge')
    bridge=network.get('Options',{}).get('com.docker.network.bridge.name','br-'+identifier[:12])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',bridge):raise ValueError('Invalid bridge interface')
    return [
        ('INPUT',['-i',bridge,'-s',str(subnet),'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT']),
        ('INPUT',['-i',bridge,'-s',str(subnet),'-d',str(gateway),'-p','tcp','--dport','18880:18949','-j','ACCEPT']),
        ('INPUT',['-i',bridge,'-s',str(subnet),'-j','DROP']),
    ]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--docker',required=True);parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    network=json.loads(subprocess.check_output([args.docker,'network','inspect','sandbox-agent-net']))[0]
    planned=rules(network)
    bridge=planned[0][1][1]
    # Native bridge-family filtering does not depend on br_netfilter/sysctls
    # and does not change routing/filtering for any other Docker bridge.
    table='das_sandbox_native'
    current=subprocess.run(['nft','list','table','bridge',table],capture_output=True,text=True)
    expression=f'meta ibrname "{bridge}" meta obrname "{bridge}" counter drop'
    if args.check:
        if current.returncode or expression.split(' counter')[0] not in current.stdout:
            raise RuntimeError('Inter-sandbox bridge guard is missing')
    else:
        prefix=f'delete table bridge {table}\n' if current.returncode==0 else ''
        script=prefix+f'table bridge {table} {{\n chain isolated {{\n type filter hook forward priority -200; policy accept;\n {expression}\n }}\n}}\n'
        subprocess.run(['nft','-f','-'],input=script,text=True,check=True)
    # Insert in reverse so the bounded accept rules precede the scoped DROP.
    for chain,rule in reversed(planned):
        tagged=[*rule[:-2],'-m','comment','--comment','sandbox-agent-native',*rule[-2:]]
        check=subprocess.run(['iptables','-w','5','-C',chain,*tagged],capture_output=True)
        if check.returncode:
            if args.check:raise RuntimeError('Native sandbox network guard is missing')
            subprocess.run(['iptables','-w','5','-I',chain,'1',*tagged],check=True)
    print(json.dumps({'network':'sandbox-agent-net','guarded':True,'mode':'check' if args.check else 'install',
                      'rules':len(planned)+1,'scope':'private bridge host ingress and inter-sandbox forwarding'}))


def verify(address,subnet):
    """Read-only prerequisite for each native allocation; no Docker socket grant."""
    devices=json.loads(subprocess.check_output(['ip','-j','address','show'],timeout=5))
    matches=[item['ifname'] for item in devices if any(info.get('local')==address for info in item.get('addr_info',[]))]
    if len(matches)!=1:raise ValueError('Native Gateway bridge address is not local')
    bridge=matches[0]
    network={'Name':'sandbox-agent-net','Internal':True,'Id':'a'*64,
        'IPAM':{'Config':[{'Subnet':subnet,'Gateway':address}]},
        'Options':{'com.docker.network.bridge.name':bridge}}
    for chain,rule in rules(network):
        tagged=[*rule[:-2],'-m','comment','--comment','sandbox-agent-native',*rule[-2:]]
        subprocess.run(['iptables','-w','5','-C',chain,*tagged],capture_output=True,timeout=8,check=True)
    current=subprocess.check_output(['nft','list','table','bridge','das_sandbox_native'],timeout=5,text=True)
    if f'meta ibrname "{bridge}" meta obrname "{bridge}"' not in current or 'drop' not in current:
        raise ValueError('Native inter-sandbox guard is missing')


if __name__=='__main__':main()
