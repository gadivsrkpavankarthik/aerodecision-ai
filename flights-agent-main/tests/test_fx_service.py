from services import fx_service
from services.fx_service import FXUnavailable, convert
from decimal import Decimal
from datetime import date

class Response:
    @property
    def text(self):
        return f'FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,SP00,A,{date.today().isoformat()},1.1\n'
    def raise_for_status(self): pass

def test_ecb_currency_conversion_decimal_and_attribution(monkeypatch):
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:Response())
    fx_service._cache.clear()
    amount,rate=convert('10.00','USD','EUR')
    assert str(amount)=='9.09'
    assert rate.source==fx_service.ECB_SOURCE
    assert rate.observation_date==date.today().isoformat()

def test_ecb_unavailable_rate_fails_closed(monkeypatch):
    class Missing(Response): text='FREQ,CURRENCY\n'
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:Missing())
    fx_service._cache.clear()
    try: convert(20,'ABC','EUR')
    except FXUnavailable: pass
    else: raise AssertionError('unknown currency must not be converted')

def test_same_currency_does_not_call_provider(monkeypatch):
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected request')))
    amount,rate=convert('12.345','INR','INR')
    assert amount==Decimal('12.345') and rate.source=='identity'

def test_stale_ecb_rate_fails_closed(monkeypatch):
    class Old(Response): text='FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,SP00,A,2026-11-01,1.1\n'
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:Old())
    fx_service._cache.clear()
    try: convert(10,'USD','EUR',max_age_days=4)
    except FXUnavailable as exc: assert 'stale' in str(exc).lower()
    else: raise AssertionError('old ECB observation must be rejected')

def test_timeout_and_invalid_ecb_rate_fail_closed(monkeypatch):
    import requests
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k: (_ for _ in ()).throw(requests.Timeout()))
    fx_service._cache.clear()
    try: convert(10,'USD','EUR')
    except FXUnavailable: pass
    else: raise AssertionError('timeout must fail closed')
    class Invalid(Response): text='FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,SP00,A,2026-11-13,0\n'
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:Invalid())
    try: convert(10,'USD','EUR')
    except FXUnavailable: pass
    else: raise AssertionError('zero rate must fail closed')

def test_multiple_currency_conversion_uses_decimal(monkeypatch):
    def response(currency, rate):
        class R(Response): pass
        R.text=f'FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\nD,{currency},EUR,SP00,A,{date.today().isoformat()},{rate}\n'
        return R()
    monkeypatch.setattr(fx_service.requests,'get',lambda url,**k:response(url.split('/D.')[1].split('.')[0],{'USD':'1.1','GBP':'0.88'}[url.split('/D.')[1].split('.')[0]]))
    fx_service._cache.clear()
    amount,rate=convert('10','USD','GBP')
    assert amount==Decimal('8.00') and rate.observation_date==date.today().isoformat()

def test_weekend_gap_is_accepted_by_four_calendar_day_policy(monkeypatch):
    class MondayDate(__import__('datetime').date):
        @classmethod
        def today(cls): return cls(2026,11,16)
    monkeypatch.setattr(fx_service,'date',MondayDate)
    class Friday(Response): text='FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\nD,USD,EUR,SP00,A,2026-11-13,1.1\n'
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:Friday())
    fx_service._cache.clear()
    amount,_=convert(11,'USD','EUR',max_age_days=4)
    assert amount==Decimal('10.00')

def test_expired_cache_entry_is_refetched(monkeypatch):
    import time
    fx_service._cache['USD']=(time.monotonic()-3601,(Decimal('1.1'),'2026-11-13'))
    calls=[]
    monkeypatch.setattr(fx_service.requests,'get',lambda *a,**k:(calls.append(1) or Response()))
    amount,_=convert(11,'USD','EUR')
    assert amount==Decimal('10.00') and len(calls)==1

def test_unsupported_currency_is_distinguished_from_provider_outage(monkeypatch):
    import requests
    def missing(*a,**k):
        response=requests.Response(); response.status_code=404
        raise requests.HTTPError('not found',response=response)
    monkeypatch.setattr(fx_service.requests,'get',missing)
    fx_service._cache.clear()
    try: convert(10,'ZZZ','EUR')
    except FXUnavailable as exc: assert 'unsupported currency' in str(exc).lower()
    else: raise AssertionError('unsupported currency must fail closed')
