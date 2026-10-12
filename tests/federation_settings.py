"""Disposable local two-database rehearsal; never a deployment settings module."""
import os
from urllib.parse import urlsplit
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from mlai.settings import *

stage_url=os.environ.get('FEDERATION_TEST_STAGING_DATABASE_URL','')
for raw in (os.environ.get('DATABASE_URL',''),stage_url):
    parsed=urlsplit(raw)
    if not DEBUG or parsed.hostname not in {'127.0.0.1','localhost'} or not parsed.path.startswith('/federation_'):
        raise ImproperlyConfigured('Federation rehearsal requires DEBUG and explicitly named local disposable databases.')
if stage_url==os.environ.get('DATABASE_URL'):
    raise ImproperlyConfigured('Federation rehearsal requires separate databases.')
DATABASES['staging']=dj_database_url.parse(stage_url)
DATABASE_ROUTERS=['tests.federation_database_support.FederationTestRouter']
