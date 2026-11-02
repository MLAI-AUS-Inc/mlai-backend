from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import NotFound

class DevelopmentBuildView(APIView):
    """Read-only developer receipt; ordinary domain route, stage only."""
    permission_classes=(IsAuthenticated,)
    def get(self,request):
        if settings.APP_ENV!='chat-staging':raise NotFound()
        return Response({'environment':settings.APP_ENV,'release':settings.APP_RELEASE})


def read_staging_status(directory=None, *, now=None, release=None):
    """Read sanitized host receipts only; never expose private controller configuration."""
    import json
    import math
    import re
    import time
    from pathlib import Path
    directory=directory or Path('/run/mlai-staging-status')
    now=time.time() if now is None else now
    release=settings.APP_RELEASE if release is None else release
    components={}
    for component in ('backend','frontend'):
        status='checking';checked=None;revision=None;action=''
        try:
            path=directory/(component+'.json')
            if path.is_symlink() or path.stat().st_size>16384:raise ValueError()
            data=json.loads(path.read_text())
            sha=data.get('active_sha','');desired=data.get('desired_sha','')
            if data.get('component')!=component or not re.fullmatch(r'[0-9a-f]{40}',sha):raise ValueError()
            checked=data.get('last_checked_at')
            if type(checked) not in (int,float) or not math.isfinite(checked):raise ValueError()
            revision=sha[:12]
            if now-checked>600 or checked>now+30:status='stale'
            elif data.get('paused') or data.get('recovery_pending') or data.get('readiness')=='needs-attention':
                status='needs-attention'
                if data.get('outcome')=='incompatible':action='A deployment change needs operator review.'
                elif data.get('reason') in {'smoke-configuration','installed-smoke'}:action='The automatic check configuration needs operator attention.'
                elif data.get('reason')=='smoke-account':action='The staging check account needs operator attention. Your account has not been changed.'
                elif data.get('reason')=='smoke-browser-access':action='Staging browser sign-in checks failed. The operator needs to investigate.'
                else:action='Automatic service checks found a problem. The operator needs to investigate.'
            elif component=='backend' and release not in (sha,sha[:12]):status='checking'
            elif desired!=sha:status='checking'
            elif data.get('readiness')=='ready':status='ready'
        except (OSError,ValueError,TypeError,AttributeError):
            checked=None
        components[component]={'status':status,'checked_at':checked,'release':revision,'message':action}
    statuses={v['status'] for v in components.values()}
    status=next((s for s in ('needs-attention','stale','checking') if s in statuses),'ready')
    messages={'ready':'Staging is ready. Sign-in and core pages passed automatic checks.',
        'checking':'Staging is updating or waiting for automatic checks. No console commands are needed.',
        'stale':'Automatic checks have stopped reporting. The operator needs to investigate.',
        'needs-attention':next((v['message'] for v in components.values() if v['message']),'A deployment needs operator review.')}
    return {'status':status,'message':messages[status],'components':components}


class DevelopmentStatusView(APIView):
    """Read-only automatic staging readiness for signed-in testers."""
    permission_classes=(IsAuthenticated,)
    def get(self,request):
        if settings.APP_ENV!='chat-staging':raise NotFound()
        response=Response(read_staging_status());response['Cache-Control']='no-store';return response
