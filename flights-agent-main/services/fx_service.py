"""ECB euro reference-rate conversion; informational, never a transaction quote."""
from __future__ import annotations
import csv, io, time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import requests
from config import config

ECB_URL='https://data-api.ecb.europa.eu/service/data/EXR/D.{currency}.EUR.SP00.A'
ECB_SOURCE='European Central Bank euro foreign exchange reference rates'
_cache={}

@dataclass(frozen=True)
class FXRate:
    rate: Decimal
    base: str
    quote: str
    source: str
    observation_date: str
    retrieved_at: str
    freshness: str

class FXUnavailable(ValueError): pass

def _euro_rate(currency: str, timeout: float=4.0, max_age_days: int|None=None) -> tuple[Decimal,str]:
    max_age_days = config.fx_max_age_days if max_age_days is None else max_age_days
    code=currency.upper()
    if code=='EUR': return Decimal('1'),date.today().isoformat()
    cached=_cache.get(code)
    if cached and time.monotonic()-cached[0]<3600:
        rate, observed=cached[1]
        age=(date.today()-date.fromisoformat(observed)).days
        if 0<=age<=max_age_days: return rate,observed
        _cache.pop(code,None)
    url=ECB_URL.format(currency=code)
    try:
        response=requests.get(url,params={'lastNObservations':1,'format':'csvdata'},headers={'Accept':'text/csv'},timeout=timeout)
        response.raise_for_status()
        rows=list(csv.DictReader(io.StringIO(response.text)))
        if not rows: raise FXUnavailable(f'ECB has no reference rate for {code}.')
        row=rows[-1]; rate=Decimal(row.get('OBS_VALUE',''))
        observed=row.get('TIME_PERIOD','')
        age=(date.today()-date.fromisoformat(observed)).days
        if not rate.is_finite() or rate<=0 or age<0 or age>max_age_days: raise FXUnavailable(f'ECB rate for {code} is invalid or stale.')
        result=(rate,observed); _cache[code]=(time.monotonic(),result); return result
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400,404):
            raise FXUnavailable(f'ECB has no reference rate for {code}; unsupported currency.') from exc
        raise FXUnavailable(f'ECB reference rate unavailable for {code}.') from exc
    except (requests.RequestException,KeyError,ValueError,ArithmeticError) as exc:
        if isinstance(exc,FXUnavailable): raise
        raise FXUnavailable(f'ECB reference rate unavailable for {code}.') from exc

def convert(amount, source: str, target: str, timeout: float=4.0, max_age_days: int|None=None) -> tuple[Decimal,FXRate]:
    max_age_days = config.fx_max_age_days if max_age_days is None else max_age_days
    value=Decimal(str(amount)); source=source.upper(); target=target.upper()
    if not value.is_finite() or value<0: raise ValueError('Amount must be a finite non-negative value.')
    if source==target:
        now=datetime.now(timezone.utc).isoformat(); return value,FXRate(Decimal('1'),source,target,'identity',date.today().isoformat(),now,'not_required')
    src,src_date=_euro_rate(source,timeout,max_age_days); dst,dst_date=_euro_rate(target,timeout,max_age_days)
    rate=dst/src
    now=datetime.now(timezone.utc).isoformat(); observation=min(src_date,dst_date)
    freshness='fresh' if (date.today()-date.fromisoformat(observation)).days<=1 else 'aging'
    return (value*rate).quantize(Decimal('0.01')),FXRate(rate,source,target,ECB_SOURCE,observation,now,freshness)
