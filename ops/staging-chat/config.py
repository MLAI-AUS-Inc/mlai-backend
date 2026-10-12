#!/usr/bin/env python3
"""Initialize independent staging credentials; never execute or print dotenv input."""
import argparse
import os
from pathlib import Path
import re
import secrets


def initialize(chat_env, output, source_sha):
    if not re.fullmatch('[0-9a-f]{40}',source_sha):
        raise ValueError('A full source commit is required.')
    if chat_env.stat().st_mode & 0o077:
        raise ValueError('The staging Chat configuration must have owner-only permissions.')
    values={}
    for line in chat_env.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key,separator,value=line.partition('=')
        if not separator or key in values:
            raise ValueError('Invalid Chat configuration.')
        values[key]=value
    token=values.get('MLAI_MEMBERSHIP_ADAPTER_TOKEN','')
    if values.get('CHAT_DOMAIN') != 'chat-staging.mlai.au' or not re.fullmatch('[0-9a-f]{64}',token):
        raise ValueError('Only the approved staging Chat adapter may be used.')
    settings=dict(STAGING_API_SOURCE_SHA=source_sha,STAGING_API_SECRET_KEY=secrets.token_hex(48),
        STAGING_API_DATABASE_PASSWORD=secrets.token_hex(32),STAGING_API_REDIS_PASSWORD=secrets.token_hex(32),
        COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET=secrets.token_hex(48),COMMUNITY_CHAT_ADAPTER_TOKEN=token,
        COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED='false')
    output.parent.mkdir(parents=True,exist_ok=True)
    with os.fdopen(os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as file:
        file.write('# Private staging API configuration. Never commit or print.\n')
        file.write(''.join(f'{key}={value}\n' for key,value in settings.items()))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['init'])
    parser.add_argument('--chat-env',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-sha',required=True)
    args=parser.parse_args()
    try:
        initialize(args.chat_env,args.output,args.source_sha)
    except (ValueError,OSError):
        parser.exit(1,'Staging API initialization refused; check staging input and preserve any existing configuration.\n')
    print('Private staging API configuration initialized with federation disabled.')
