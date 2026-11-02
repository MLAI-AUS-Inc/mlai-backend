#!/usr/bin/env python3
"""Pinned host-only staging sign-in probe. Never prints credentials or response bodies."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import urllib.request
import urllib.error
import uuid

API='https://api-staging.mlai.au/api/v1'
ORIGIN='https://chat-staging.mlai.au'
CREDENTIALS=Path('/etc/mlai-staging-smoke/credentials.json')

class SmokeFailure(Exception):
    pass


def read_credentials(path=CREDENTIALS):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.geteuid() or path.stat().st_mode & 0o077:
        raise SmokeFailure('credentials')
    try:data=json.loads(path.read_text())
    except (OSError,ValueError):raise SmokeFailure('credentials') from None
    if not isinstance(data,dict):raise SmokeFailure('credentials')
    return data


def validate_response(status,headers):
    if status!=200:raise SmokeFailure('http')
    if headers.get('Access-Control-Allow-Origin')!=ORIGIN or headers.get('Access-Control-Allow-Credentials')!='true':
        raise SmokeFailure('browser-access')


def failure_category(error):
    code=str(error) if isinstance(error,SmokeFailure) else ''
    return {'credentials':'configuration','account':'account','release':'release',
        'core-pages':'core-pages','browser-access':'browser-access','cookies':'browser-access'}.get(code,'service')


def validate_payload(path,data,email):
    if path=='/auth/me/':
        if not isinstance(data,dict) or str(data.get('email','')).casefold()!=email.casefold():raise SmokeFailure('account')
    elif path=='/community-chat/account/':
        if not isinstance(data,dict) or data.get('authenticated') is not True or not isinstance(data.get('profile'),dict):raise SmokeFailure('core-pages')
    elif path=='/community-chat/home/':
        if not isinstance(data,dict) or not isinstance(data.get('points'),dict) or not isinstance(data.get('feature_flags'),dict):raise SmokeFailure('core-pages')
    elif path=='/community-chat/mentors/':
        if not isinstance(data,dict) or not isinstance(data.get('mentors'),list):raise SmokeFailure('core-pages')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise SmokeFailure('redirect')


class BrowserSession:
    def __init__(self):
        self.jar=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPCookieProcessor(self.jar))

    def request(self,path,body=None):
        # Paths come only from check(), never config, command line, or API responses.
        if not path.startswith('/') or path.startswith('//') or '..' in path or '?' in path:
            raise SmokeFailure('path')
        request=urllib.request.Request(API+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Origin':ORIGIN,'Content-Type':'application/json','User-Agent':'MLAI-staging-readiness/1'})
        try:
            with self.opener.open(request,timeout=12) as response:
                validate_response(response.status,response.headers)
                data=json.loads(response.read(1024*1024))
            if path.endswith('/password/'):
                cookies={c.name:c for c in self.jar}
                for name in ('__Host-mlai_staging_access','__Host-mlai_staging_refresh'):
                    c=cookies.get(name)
                    if c is None or not c.secure or c.domain!='api-staging.mlai.au' or c.domain_specified or c.path!='/' or not c.has_nonstandard_attr('HttpOnly'):
                        raise SmokeFailure('cookies')
            return data
        except SmokeFailure:raise
        except urllib.error.HTTPError as error:
            raise SmokeFailure('account' if error.code in (401,403) else 'service') from None
        except Exception:raise SmokeFailure('request') from None


def check(credentials,expected_sha,request):
    if (not re.fullmatch(r'[0-9a-f]{40}',expected_sha or '')
        or not re.fullmatch(r'[^@\s]+@staging\.mlai\.au',credentials.get('email') or '')
        or not credentials.get('password')
        or not re.fullmatch(r'[0-9a-f]{64}',credentials.get('public_key') or '')):
        raise SmokeFailure('credentials')
    try:uuid.UUID(credentials['installation_id'])
    except (KeyError,ValueError,TypeError):raise SmokeFailure('credentials') from None
    request('/community-chat/auth/password/',{
        'email':credentials['email'],'password':credentials['password'],'client_id':'mlai-chat-web',
        'device':{'public_key':credentials['public_key'],'installation_id':credentials['installation_id'],'platform':'web','name':'Staging readiness'}})
    try:
        for path in ('/auth/me/','/community-chat/account/','/community-chat/home/','/community-chat/mentors/'):
            validate_payload(path,request(path),credentials['email'])
        build=request('/points/development/build/')
        if build.get('environment')!='chat-staging' or build.get('release') not in (expected_sha,expected_sha[:12]):
            raise SmokeFailure('release')
        request('/community-chat/auth/session/refresh/',{})
        request('/auth/me/')
        return {'ok':True,'check':'signed-in','release':build['release']}
    finally:
        request('/community-chat/auth/session/logout/',{})


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--sha',required=True);parser.add_argument('--report',action='store_true');args=parser.parse_args()
    try:
        print(json.dumps(check(read_credentials(),args.sha,BrowserSession().request)));return 0
    except Exception as error:
        print(json.dumps({'ok':False,'check':'signed-in','category':failure_category(error)}));return 0 if args.report else 1

if __name__=='__main__':raise SystemExit(main())
