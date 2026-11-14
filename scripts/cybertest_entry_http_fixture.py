"""Synthetic loopback authority for browser acceptance. No Django, DB or secrets.

Never deploy this fixture: its account is intentionally synthetic and predefined.
"""
import json
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from roo.cybertest_entry import EntryAuthority, EntryError

COMPANY='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
WORKSPACE='20000000-0000-4000-8000-000000000001'
KEY='local-fixture-only-not-a-secret-123456'
revoked=False
class Cache:
    def __init__(self): self.values={}; self.lock=Lock()
    def get(self,key): return self.values.get(key)
    def set(self,key,value,timeout): self.values[key]=value
    def add(self,key,value,timeout):
        with self.lock:
            if key in self.values: return False
            self.values[key]=value
            return True
def authorize(session,company):
    if revoked or session!='fixture-session' or company!=COMPANY:
        raise EntryError('ACCESS_REVOKED','Synthetic account unavailable.',401)
    return dict(user_id='17',company_id=COMPANY,workspace_id=WORKSPACE,company_name='Acme Trees')
authority=EntryAuthority(Cache(),authorize)
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        global revoked
        status=200
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384: raise EntryError('INVALID_REQUEST','Invalid fixture input.')
            body=json.loads(self.rfile.read(length))
            if self.path=='/authorize':
                result={'code':authority.issue('fixture-session','17',body['company_id'],body['state'],body['code_challenge'])}
            elif self.path=='/revoke': revoked=True; result={'revoked':True}
            else:
                if self.headers.get('X-Cybertest-Key')!=KEY: raise EntryError('UNAUTHORIZED','Fixture key required.',403)
                if self.path.endswith('/exchange/'): result=authority.exchange(**body)
                elif self.path.endswith('/inspect/'): result=authority.inspect(body['grant'])
                else: raise EntryError('NOT_FOUND','Unknown fixture endpoint.',404)
        except EntryError as exc: status=exc.status;result={'code':exc.code}
        except Exception: status=400;result={'code':'INVALID_FIXTURE_INPUT'}
        content=json.dumps(result).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(json.dumps({'port':server.server_port}),flush=True)
server.serve_forever()
