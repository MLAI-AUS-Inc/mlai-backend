#!/usr/bin/env python3
"""Initialize independent staging credentials; never execute or print dotenv input."""
import argparse
import json
import tempfile
import os
from pathlib import Path
import re
import secrets
import base64


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
        COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED='false',STAGING_API_IMAGE_ID='',**new_keyring())
    output.parent.mkdir(parents=True,exist_ok=True)
    with os.fdopen(os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as file:
        file.write('# Private staging API configuration. Never commit or print.\n')
        file.write(''.join(f'{key}={value}\n' for key,value in settings.items()))


FIELDS={'STAGING_API_SOURCE_SHA','STAGING_API_SECRET_KEY','STAGING_API_DATABASE_PASSWORD',
    'STAGING_API_REDIS_PASSWORD','COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET','COMMUNITY_CHAT_ADAPTER_TOKEN',
    'COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED','STAGING_API_IMAGE_ID',
    'CONNECTOR_CREDENTIAL_KEYS','CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID'}
KEYRING_FIELDS={'CONNECTOR_CREDENTIAL_KEYS','CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID'}


def new_keyring():
    return {'CONNECTOR_CREDENTIAL_KEYS':json.dumps({'staging-v1':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()},separators=(',',':')),
            'CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID':'staging-v1'}


def validate_keyring(values):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate keyring ID.')
            result[key]=value
        return result
    keys=json.loads(values['CONNECTOR_CREDENTIAL_KEYS'],object_pairs_hook=unique)
    active=values['CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID']
    if not isinstance(keys,dict) or not keys or active not in keys:raise ValueError('Invalid keyring.')
    for identifier,key in keys.items():
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',identifier) or not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{43}=',key):raise ValueError('Invalid keyring.')
        if len(base64.urlsafe_b64decode(key))!=32:raise ValueError('Invalid keyring.')


def write_private(path,values):
    descriptor,temporary=tempfile.mkstemp(dir=path.parent)
    try:
        if path.exists():os.fchmod(descriptor,path.stat().st_mode & 0o777)
        with os.fdopen(descriptor,'w') as output:output.write('# Private isolated staging API configuration.\n'+''.join(f'{key}={value}\n' for key,value in sorted(values.items())))
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def upgrade_keyring(path):
    values=read_config(path,allow_legacy_keyring=True)
    if KEYRING_FIELDS <= values.keys():return
    values.update(new_keyring());validate_keyring(values);write_private(path,values)


def read_config(path,allow_legacy_keyring=False):
    if path.stat().st_mode & 0o077: raise ValueError('Owner-only configuration required.')
    values={}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):continue
        key,separator,value=line.partition('=')
        if not separator or key in values:raise ValueError('Invalid configuration.')
        values[key]=value
    if set(values)!=FIELDS and not (allow_legacy_keyring and set(values)==FIELDS-KEYRING_FIELDS):raise ValueError('Unexpected configuration fields.')
    if not re.fullmatch('[0-9a-f]{40}',values['STAGING_API_SOURCE_SHA']):raise ValueError('Invalid source.')
    for name in FIELDS-KEYRING_FIELDS-{'STAGING_API_SOURCE_SHA','STAGING_API_IMAGE_ID','COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED'}:
        length=96 if name in {'STAGING_API_SECRET_KEY','COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET'} else 64
        if not re.fullmatch('[0-9a-f]{'+str(length)+'}',values[name]):raise ValueError('Invalid isolated credential.')
    if len({values[name] for name in FIELDS-KEYRING_FIELDS if name not in {'STAGING_API_SOURCE_SHA','STAGING_API_IMAGE_ID','COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED'}})!=5:raise ValueError('Credentials must be independent.')
    if values['COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED'] not in {'true','false'}:raise ValueError('Invalid federation flag.')
    if values['STAGING_API_IMAGE_ID'] and not re.fullmatch('sha256:[0-9a-f]{64}',values['STAGING_API_IMAGE_ID']):raise ValueError('Invalid image identity.')
    if KEYRING_FIELDS <= values.keys():validate_keyring(values)
    return values


def image_id(values,metadata):
    if not isinstance(metadata,list) or len(metadata)!=1:raise ValueError('Expected one image.')
    labels=metadata[0].get('Config',{}).get('Labels') or {}
    if labels.get('org.opencontainers.image.revision')!=values['STAGING_API_SOURCE_SHA'] or labels.get('au.mlai.environment')!='chat-staging-api':raise ValueError('Image provenance mismatch.')
    identifier=metadata[0].get('Id','')
    if not re.fullmatch('sha256:[0-9a-f]{64}',identifier):raise ValueError('Invalid image identity.')
    if values['STAGING_API_IMAGE_ID'] and values['STAGING_API_IMAGE_ID']!=identifier:raise ValueError('Image changed since build.')
    return identifier


def record_build(path,metadata,source_sha):
    values=read_config(path)
    if not re.fullmatch('[0-9a-f]{40}',source_sha):raise ValueError('Invalid source.')
    values.update(STAGING_API_SOURCE_SHA=source_sha,STAGING_API_IMAGE_ID='')
    values['STAGING_API_IMAGE_ID']=image_id(values,metadata)
    write_private(path,values)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['init','validate','record-build','check-image','upgrade-keyring'])
    parser.add_argument('--chat-env',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-sha')
    parser.add_argument('--metadata',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='init':
            if not args.chat_env or not args.source_sha:raise ValueError('Missing initialization context.')
            initialize(args.chat_env,args.output,args.source_sha)
        elif args.action=='validate':read_config(args.output)
        elif args.action=='upgrade-keyring':upgrade_keyring(args.output)
        elif args.action=='record-build':
            if not args.metadata or not args.source_sha:raise ValueError('Missing build context.')
            record_build(args.output,json.loads(args.metadata.read_text()),args.source_sha)
        else:
            if not args.metadata:raise ValueError('Missing image context.')
            image_id(read_config(args.output),json.loads(args.metadata.read_text()))
    except (ValueError,OSError,KeyError,TypeError):
        parser.exit(1,'Staging API configuration/image check refused; preserve private configuration and verify its staging provenance.\n')
    print('Staging API configuration/image check passed; no credentials printed.')
