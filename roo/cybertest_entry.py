"""Browser-bound Cybertest entry contracts, independent of Django storage."""
import base64
import hashlib
import hmac
import re
import secrets
import time
from uuid import UUID


class EntryError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value):
        raise EntryError('INVALID_ENTRY', 'Restart sign-in from your MLAI company.')
    return value


def _key(kind, token):
    return 'cybertest:entry:' + kind + ':' + hashlib.sha256(_token(token).encode()).hexdigest()


def _identity(value):
    try:
        result = dict(user_id=str(value['user_id']), company_id=str(UUID(value['company_id'])),
                    workspace_id=str(UUID(value['workspace_id'])), company_name=str(value['company_name'])[:120])
        if value.get('workspace_provisioning') is True: result['workspace_provisioning'] = True
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise EntryError('ENTRY_UNAVAILABLE', 'Company access is unavailable.', 503) from exc


class EntryAuthority:
    """Issue single-use codes; recheck authority before exchange and each grant use.

    cache.add must be atomic across all workers. Cache contents contain opaque
    session references, never account access/refresh tokens or GitHub secrets.
    """
    def __init__(self, cache, authorize, *, clock=time.time):
        self.cache, self.authorize, self.clock = cache, authorize, clock

    def issue(self, session_id, user_id, company_id, state, challenge):
        _token(state)
        _token(challenge)
        if len(challenge) != 43:
            raise EntryError('INVALID_ENTRY', 'Use an S256 login challenge.')
        try: company_id = str(UUID(str(company_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise EntryError('INVALID_COMPANY', 'Choose a valid company.') from exc
        identity = _identity(self.authorize(session_id, company_id))
        if identity['user_id'] != str(user_id) or identity['company_id'] != company_id:
            raise EntryError('WRONG_ACCOUNT', 'Restart sign-in with the selected account.', 403)
        code = secrets.token_urlsafe(32)
        self.cache.set(_key('code', code), dict(identity=identity, session_id=str(session_id),
            state=state, challenge=challenge, expires=self.clock()+120), timeout=120)
        return code

    def _load(self, kind, token):
        row = self.cache.get(_key(kind, token))
        if not row or row['expires'] <= self.clock():
            raise EntryError('ENTRY_EXPIRED', 'Sign in again from MLAI.', 401)
        identity = _identity(self.authorize(row['session_id'], row['identity']['company_id']))
        if any(identity[k] != row['identity'][k] for k in ('user_id','company_id','workspace_id')):
            raise EntryError('ACCESS_CHANGED', 'Company access changed. Sign in again.', 403)
        return row, identity

    def exchange(self, code, state, verifier):
        _token(state)
        _token(verifier)
        row, identity = self._load('code', code)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if not hmac.compare_digest(row['state'], state) or not hmac.compare_digest(row['challenge'], challenge):
            raise EntryError('INVALID_ENTRY', 'Restart sign-in in the original browser.', 403)
        if not self.cache.add(_key('used', code), True, timeout=120):
            raise EntryError('ENTRY_USED', 'This sign-in link was already used.', 409)
        grant = secrets.token_urlsafe(32)
        self.cache.set(_key('grant', grant), dict(identity=identity, session_id=row['session_id'],
            expires=self.clock()+3600), timeout=3600)
        return {**identity, 'grant': grant}

    def inspect(self, grant):
        _, identity = self._load('grant', grant)
        return identity
