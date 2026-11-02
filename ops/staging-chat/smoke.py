#!/usr/bin/env python3
"""Pinned host-only staging sign-in probe. Never prints credentials or response bodies."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import urllib.request
import uuid

API='https://api-staging.mlai.au/api/v1'
ORIGIN='https://chat-staging.mlai.au'
CREDENTIALS=Path('/etc/mlai-staging-smoke/credentials.json')

class SmokeFailure(Exception):
    pass


def read_credentials(path=CREDENTIALS):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.geteuid() or path.stat().st_mode & 0o077:
        raise SmokeFailure('credentials')
    return json.loads(path.read_text())


def validate_response(status,headers):
    if status!=200:raise SmokeFailure('http')
    if headers.get('Access-Control-Allow-Origin')!=ORIGIN or headers.get('Access-Control-Allow-Credentials')!='true':
        raise SmokeFailure('browser-access')


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
            request(path)
        build=request('/points/development/build/')
        if build.get('environment')!='chat-staging' or build.get('release') not in (expected_sha,expected_sha[:12]):
            raise SmokeFailure('release')
        request('/community-chat/auth/session/refresh/',{})
        request('/auth/me/')
        return {'ok':True,'check':'signed-in','release':build['release']}
    finally:
        request('/community-chat/auth/session/logout/',{})


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--sha',required=True);args=parser.parse_args()
    try:
        print(json.dumps(check(read_credentials(),args.sha,BrowserSession().request)));return 0
    except Exception:
        print(json.dumps({'ok':False,'check':'signed-in'}));return 1

if __name__=='__main__':raise SystemExit(main())
