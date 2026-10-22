import asyncio
import base64
import json
import time

import httpx
import pytest

from iacpaas_mcp import backend, auth, credentials


def token(exp):
    return 'header.'+base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')+'.signature'


@pytest.fixture
def saved(monkeypatch):
    monkeypatch.delenv('IACPAAS_API_KEY',raising=False)
    monkeypatch.delenv('IACPAAS_JWT',raising=False)
    monkeypatch.setenv('IACPAAS_BASE_URL','https://iacpaas.dvo.ru')
    stale=token(time.time()-60);fresh=token(time.time()+600)
    record={'username':'test','password':'never-pass-to-llm','token':stale}
    monkeypatch.setattr(backend,'load_account',lambda:record)
    calls=[]
    monkeypatch.setattr(auth,'signin',lambda user,password:(calls.append((user,password)) or fresh))
    monkeypatch.setattr(credentials,'save_account',lambda *args:None)
    return stale,fresh,calls


def test_expired_saved_token_refreshes_once_across_parallel_reads(saved):
    stale,fresh,calls=saved
    def reply(request):
        assert request.headers['Authorization']=='Bearer '+fresh
        return httpx.Response(200,json={'ok':True})
    b=backend.HttpBackend(transport=httpx.MockTransport(reply))
    async def run():return await asyncio.gather(*(b.request('GET','/test') for _ in range(3)))
    assert len(asyncio.run(run()))==3 and len(calls)==1


def test_expired_environment_token_requires_user_refresh(monkeypatch):
    monkeypatch.delenv('IACPAAS_API_KEY',raising=False)
    monkeypatch.setenv('IACPAAS_JWT',token(time.time()-60))
    b=backend.HttpBackend(transport=httpx.MockTransport(lambda r:pytest.fail('Expired JWT must not be sent')))
    with pytest.raises(backend.PlatformError) as caught:asyncio.run(b.request('GET','/test'))
    assert caught.value.code=='authentication_required' and not caught.value.retryable


def test_rejected_mutation_is_not_replayed(saved):
    _,fresh,calls=saved
    b=backend.HttpBackend(transport=httpx.MockTransport(lambda r:httpx.Response(401)))
    b.headers['Authorization']='Bearer '+fresh
    with pytest.raises(backend.PlatformError):asyncio.run(b.request('PUT','/test',json={'value':1}))
    assert not calls


def test_failed_login_is_sanitized_and_not_reported_as_bad_path(saved,monkeypatch):
    def fail(*args):raise ValueError('secret-provider-error-body')
    monkeypatch.setattr(auth,'signin',fail)
    b=backend.HttpBackend(transport=httpx.MockTransport(lambda r:pytest.fail('Must not send failed token')))
    with pytest.raises(backend.PlatformError) as caught:asyncio.run(b.request('GET','/test'))
    assert caught.value.code=='authentication_required'
    assert 'secret' not in str(caught.value) and 'never-pass' not in str(caught.value)


def test_refresh_never_switches_to_a_different_saved_account(saved,monkeypatch):
    _,_,calls=saved
    b=backend.HttpBackend(transport=httpx.MockTransport(lambda r:pytest.fail('Must not switch account')))
    monkeypatch.setattr(backend,'load_account',lambda:{'username':'other','password':'other-password'})
    with pytest.raises(backend.PlatformError) as caught:
        asyncio.run(b.request('GET','/test'))
    assert caught.value.code=='authentication_required' and not calls


def test_http_200_expired_token_error_retries_only_after_refresh(saved):
    _,fresh,calls=saved
    sent=[]
    def reply(request):
        sent.append(request.headers['Authorization'])
        if len(sent)==1:return httpx.Response(200,json={'error':'JWT token expired'})
        return httpx.Response(200,json={'ok':True})
    b=backend.HttpBackend(transport=httpx.MockTransport(reply))
    # A rejected but not locally expired token exercises the reactive refresh path.
    b.headers['Authorization']='Bearer opaque-rejected-token'
    assert asyncio.run(b.request('GET','/test'))=={'ok':True}
    assert sent==['Bearer opaque-rejected-token','Bearer '+fresh] and len(calls)==1


def test_platform_update_dispatch_error_is_diagnostic_and_never_retried(monkeypatch):
    monkeypatch.setenv('IACPAAS_API_KEY','test-key')
    sent=[]
    def reply(request):
        sent.append(request)
        return httpx.Response(200,json={'success':False,'error':'Ошибка',
            'explanation':'Неожиданное значение параметра action: updateVertice'})
    b=backend.HttpBackend(transport=httpx.MockTransport(reply))
    with pytest.raises(backend.PlatformError) as caught:
        asyncio.run(b.request('PUT','/api/data/update',json={'path':'own-demo'}))
    assert caught.value.code=='platform_operation_unavailable' and not caught.value.retryable
    assert len(sent)==1 and 'другие пути' in caught.value.suggestion
