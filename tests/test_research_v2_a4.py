"""Synthetic observer/readiness tests. No provider or model requests leave the process."""
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from news.evidence_capture import _observer, active, observe, publication_iso
from research_v2.capture import EvidenceCapture, import_historical
from research_v2.evidence import build_packet
from research_v2.readiness import assess_readiness
from research_v2.artifacts import assess_coverage, load_archive


def row(day='2026-10-28', url='https://example.com/article'):
    return {'title':'Company forecast preview', 'description':'Original supplied excerpt, not generated text.',
            'source':'Synthetic Publisher','url':url,'published_at':day+'T15:00:00Z','entities':[{'symbol':'ORCL'}]}


def response(payload):
    return SimpleNamespace(status_code=200,json=lambda:payload,raise_for_status=lambda:None)


def test_marketaux_capture_precedes_semantic_filter_without_extra_requests(tmp_path,monkeypatch):
    import news.marketaux as module
    monkeypatch.setattr(module,'CACHE_DIR',tmp_path/'cache')
    calls=[]
    def transport(*a,**kw):
        calls.append(kw['params'].copy())
        return response({'data':[row()]})
    client=module.MarketauxDiscovery(logging.getLogger('test'),'2026-10-28',token='PRIVATE_UNIT_TOKEN',request_get=transport)
    expected=client.search('Company forecast','GENERAL')
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        actual=client.search('Company forecast','GENERAL')
    packet=capture.finish()
    assert expected==actual==[] and len(calls)==1  # second call reused the existing cache
    assert len(packet['evidence'])==1
    assert packet['evidence'][0]['source_text']==row()['description']
    assert 'PRIVATE_UNIT_TOKEN' not in (capture.root/'v2_captured_recordings.json').read_text()
    assert not active()


def test_finnhub_raw_and_cache_capture_preserve_original_text(tmp_path):
    from news.providers.finnhub import FinnhubProvider
    calls=[]
    payload=[{'headline':'Unfiltered company preview','source':'Synthetic Publisher','url':'https://example.com/f',
              'datetime':1793199600,'summary':'Original Finnhub summary.'}]
    def transport(*a,**kw):calls.append(1);return response(payload)
    provider=FinnhubProvider(token='unit-token',request_get=transport,cache_dir=tmp_path/'cache')
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        first=provider.search_company('ORCL','2026-10-28','2026-10-29')
        second=provider.search_company('ORCL','2026-10-28','2026-10-29')
    assert len(calls)==1 and first==second
    assert capture.manifest['capture']['retained']==1
    assert capture.finish()['evidence'][0]['source_text']=='Original Finnhub summary.'


def test_official_rss_preserves_original_time_and_excerpt(tmp_path):
    from news.official import _rss_candidates
    stamp='Wed, 28 Oct 2026 15:00:00 GMT'
    xml=f'<rss><channel><item><title>Unclassified release</title><link>https://example.com/release</link><pubDate>{stamp}</pubDate><description>Original RSS summary.</description></item></channel></rss>'
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        candidates=_rss_candidates(xml,'BLS','bls.gov','https://www.bls.gov/feed','MACRO','2026-10-28')
    item=capture.finish()['evidence'][0]
    assert item['publication_timestamp_original']==stamp
    assert item['source_text']=='Original RSS summary.'
    assert not candidates[0].editorial_eligible
    assert publication_iso('2026-10-28T15:00:00') is None


def test_sec_exhibit_capture_from_existing_cache_boundary(tmp_path,monkeypatch):
    import news.verification_acquisition as module
    monkeypatch.setattr(module,'CACHE_DIR',tmp_path/'cache')
    acquirer=module.LiveEvidenceAcquirer(logging.getLogger('test'),'2026-10-28',request_get=lambda *a,**kw:pytest.fail('network'))
    candidate={'title':'Filing','source_url':'https://example.com/filing'}
    data=[{'title':'SEC 8-K','source_domain':'sec.gov','source_url':'https://www.sec.gov/Archives/unit',
           'published_at':'2026-10-28T15:00:00Z','affected_symbols':['ORCL'],'filing_form':'8-K','exhibit_text':'Original exhibit excerpt.'}]
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        acquirer._save_cache(candidate,'sec_ORCL',data)
        assert acquirer._load_cache(candidate,'sec_ORCL')==data
    item=capture.finish()['evidence'][0]
    assert item['filing_metadata']['exhibit_text']=='Original exhibit excerpt.'
    assert item['source_text']=='Original exhibit excerpt.'


def test_budget_dedup_and_missing_time_are_explicit(tmp_path):
    capture=EvidenceCapture('2026-10-28',tmp_path/'capture',max_items=1,synthetic=True)
    capture.record('marketaux',{'data':[row(),row(),row(url='https://example.com/other')]})
    capture.record('finnhub',[{'title':'Undated','source':'Publisher','url':'https://example.com/undated'}])
    assert capture.manifest['capture']['retained']==1
    assert capture.manifest['capture']['duplicates']==1
    assert capture.manifest['capture']['budget_excluded']==1
    assert capture.manifest['recordings'][-1]['hygiene_rejections']
    assert not list(capture.root.glob('*.tmp'))


def test_observer_failure_never_changes_provider_control_flow(caplog):
    def failed(*a,**kw):raise RuntimeError('PRIVATE_ERROR_VALUE')
    token=_observer.set(failed)
    try:observe('test',[])
    finally:_observer.reset(token)
    assert 'V2_CAPTURE_FAILED' in caplog.text and 'PRIVATE_ERROR_VALUE' not in caplog.text


def test_historical_import_is_dated_read_only_and_does_not_reuse_latest(tmp_path):
    root=tmp_path/'cache';old=root/'marketaux'/'2026-10-22';old.mkdir(parents=True)
    source=old/'general.json';source.write_text(json.dumps({'data':[row(day='2026-10-22')]}))
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    missing=import_historical('2026-10-28',tmp_path/'missing',cache_root=root)
    assert missing.manifest['capture']['retained']==0
    found=import_historical('2026-10-22',tmp_path/'found',cache_root=root)
    assert found.manifest['capture']['retained']==1
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    assert found.manifest['source_files'][0]['sha256']==before


def test_historical_official_rss_import_has_no_fetch(tmp_path):
    root=tmp_path/'cache';folder=root/'official_news'/'2026-10-28';folder.mkdir(parents=True)
    (folder/'bls.xml').write_text('<rss><channel><item><title>Release</title><link>https://www.bls.gov/news/unit</link><pubDate>Wed, 28 Oct 2026 15:00:00 GMT</pubDate><description>Original supplied RSS excerpt.</description></item></channel></rss>')
    capture=import_historical('2026-10-28',tmp_path/'capture',cache_root=root)
    assert capture.manifest['capture']['retained']==1


def ready_packet():
    """Mock a genuine acquisition contract; never exported as actual research evidence."""
    packet=build_packet('2026-10-28',[{'provider':'marketaux','coverage_state':'AVAILABLE','payload':{'data':[row(url='https://www.reuters.com/unit-test-fictional-path')]}}],
        movers=[{'symbol':'ORCL','change_pct':10.73},{'symbol':'NOW','change_pct':-3}],
        market_context={'indices':[{'symbol':'SPY','change_pct':1}],'sectors':{'all':[{'symbol':'XLK','change_pct':1}]},'peer_membership':{'technology':['ORCL']}})
    packet['diagnostics'].update(origin='REAL_ARCHIVED_REPORT',source_files=[{'sha256':'mock-provenance-contract-only'}])
    return packet


def readiness(packet,**kwargs):
    return assess_readiness(packet,assess_coverage(packet),**kwargs)


def test_meaningful_bounded_research_is_ready_without_claiming_full_coverage():
    result=readiness(ready_packet())
    assert result['status']=='READY_FOR_LLM_RESEARCH'
    assert result['uncovered_movers']==['NOW'] and not result['coverage_complete']
    assert result['text_supported_movers']==['ORCL']


@pytest.mark.parametrize('change,expected',[
    ('synthetic','INSUFFICIENT_SOURCE_EVIDENCE'),('timestamp','INSUFFICIENT_SOURCE_EVIDENCE'),
    ('peer','PROVIDER_COVERAGE_GAP'),('interrupted','PROVIDER_COVERAGE_GAP'),
    ('capture-budget','BUDGET_EXCEEDED')])
def test_readiness_fail_closed(change,expected):
    packet=ready_packet()
    if change=='synthetic':packet['diagnostics']['origin']='SYNTHETIC_TEST_FIXTURE'
    elif change=='timestamp':packet['evidence'][0]['published_at']='invented'
    elif change=='peer':packet['market_context']['peer_membership']={}
    elif change=='interrupted':packet['diagnostics']['capture']={'report_pipeline_completed':False}
    else:packet['diagnostics']['capture']={'budget_excluded':1}
    assert readiness(packet)['status']==expected


def test_token_limit_and_reserved_fixture_hosts():
    assert readiness(ready_packet(),max_input_tokens=1)['status']=='BUDGET_EXCEEDED'
    p=build_packet('2026-10-28',[{'provider':'marketaux','coverage_state':'AVAILABLE','payload':{'data':[row()]}}])
    p['diagnostics'].update(origin='REAL_ARCHIVED_REPORT',source_files=[{'sha256':'unit'}])
    assert readiness(p)['status']=='INSUFFICIENT_SOURCE_EVIDENCE'


def test_synthetic_recordings_cannot_be_imported_as_real(tmp_path):
    from tests.test_research_v2_a3 import archive
    capture=EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True)
    capture.flush()
    with pytest.raises(ValueError,match='SYNTHETIC_RECORDINGS_NOT_REAL'):
        load_archive(archive(tmp_path),'2026-10-28',capture.root/'v2_captured_recordings.json')


@pytest.mark.parametrize('fail',[False,True])
def test_report_only_capture_preserves_pipeline_and_resets_after_failure(tmp_path,monkeypatch,fail):
    import run_daily
    from tests.test_run_daily import _stub_pipeline
    _stub_pipeline(monkeypatch,tmp_path)
    monkeypatch.setattr(run_daily,'load_dotenv',lambda *a,**kw:None)
    monkeypatch.setattr(run_daily,'configure_logging',lambda:logging.getLogger('capture-unit'))
    monkeypatch.setenv('ENABLE_LLM','0')
    def driver(*a,**kw):
        observe('marketaux',{'data':[row(day='2026-09-25')]})
        if fail:raise RuntimeError('UNIT_PIPELINE_FAILURE')
    monkeypatch.setattr(run_daily,'run_market_driver_stage',driver)
    result=run_daily.run(['--dry-run','--v2-evidence-output',str(tmp_path/'capture')])
    assert result==(1 if fail else 0) and not active()
    manifest=json.loads((tmp_path/'capture'/'v2_captured_recordings.json').read_text())
    assert manifest['capture']['retained']==1
    assert manifest['capture']['report_pipeline_completed'] is (not fail)


@pytest.mark.parametrize('dry,llm',[(False,False),(True,True)])
def test_capture_requires_no_send_and_disabled_paid_narrative(tmp_path,monkeypatch,dry,llm):
    import run_daily
    monkeypatch.setattr(run_daily,'configure_logging',lambda:logging.getLogger('test'))
    monkeypatch.setattr(run_daily,'load_dotenv',lambda *a,**kw:None)
    monkeypatch.setattr(run_daily,'load_llm_settings',lambda:{'enable_llm':llm})
    monkeypatch.setattr(run_daily,'write_status',lambda *a:None)
    monkeypatch.setattr(run_daily,'run_preflight',lambda *a:pytest.fail('must block before collection'))
    assert run_daily.run((['--dry-run'] if dry else [])+['--v2-evidence-output',str(tmp_path/'capture')])==1


def test_usage_limit_capture_has_explicit_state_and_no_extra_requests(tmp_path,monkeypatch):
    import requests
    import news.marketaux as module
    monkeypatch.setattr(module,'CACHE_DIR',tmp_path/'cache')
    calls=[]
    def transport(*a,**kw):
        calls.append(1)
        r=requests.Response();r.status_code=402;r._content=b'{"error":{"code":"usage_limit_reached"}}';return r
    client=module.MarketauxDiscovery(logging.getLogger('test'),'2026-10-28',token='unit',request_get=transport)
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        assert client.search('', 'TARGETED_MOVER',['ORCL'])==[]
        assert client.search('', 'TARGETED_MOVER',['MU'])==[]
    assert len(calls)==1
    assert all(r['coverage_state']=='QUOTA_EXHAUSTED' for r in capture.manifest['recordings'])
    assert capture.manifest['recordings'][-1]['requested_symbols']==['MU']


def test_source_url_credentials_and_invalid_publisher_are_not_persisted(tmp_path):
    capture=EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True)
    capture.record('marketaux',{'data':[row(url='https://example.com/article?api_token=PRIVATE_UNIT_VALUE'),{**row(),'source':None}]})
    text=(capture.root/'v2_captured_recordings.json').read_text()
    assert 'PRIVATE_UNIT_VALUE' not in text and capture.manifest['capture']['retained']==0


def test_company_ir_text_is_captured_without_an_extra_http_call(tmp_path,monkeypatch):
    import news.verification_acquisition as module
    monkeypatch.setattr(module,'CACHE_DIR',tmp_path/'cache')
    calls=[]
    html='<html><title>Original issuer announcement</title><meta property="article:published_time" content="2026-10-28T15:00:00Z"><p>Original source words.</p></html>'
    def transport(*a,**kw):calls.append(1);return SimpleNamespace(status_code=200,text=html,raise_for_status=lambda:None)
    acquirer=module.LiveEvidenceAcquirer(logging.getLogger('test'),'2026-10-28',request_get=transport)
    acquirer.company_sources={'ORCL':{'ir_url':'https://example.com/ir'}}
    from news.verification_v2 import VerificationDiagnostics
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        acquirer._company_ir({'affected_symbols':['ORCL'],'title':'Candidate fallback','published_at':'2026-10-28T14:00:00Z'},VerificationDiagnostics())
    assert len(calls)==1
    assert any('Original source words.' in e['source_text'] for e in capture.finish()['evidence'])


def test_real_llm_is_blocked_before_transport_when_readiness_fails(tmp_path,monkeypatch):
    from tests.test_research_v2_a3 import archive
    from research_v2.session import generate_package
    import research_v2.session as module
    monkeypatch.setenv('OPENAI_API_KEY','SYNTHETIC_UNIT_KEY')
    monkeypatch.setattr(module,'OpenAIResearchProvider',lambda **kw:pytest.fail('must not call paid transport'))
    result=generate_package(mode='real-llm',output=tmp_path/'out',archive=archive(tmp_path),market_date='2026-10-28',authorized=True,max_spend_usd='.03')
    assert result['status']=='BLOCKED' and result['usage']['api_calls']==0
    assert result['readiness']['status']=='PROVIDER_COVERAGE_GAP'


def test_page_capture_does_not_guess_dates_or_include_script_metadata():
    from news.evidence_capture import page_excerpt
    title,stamp,original,text=page_excerpt('<html><head><title>Original title</title></head><body><script>PRIVATE_SCRIPT</script><p>Original paragraph.</p></body></html>')
    assert title=='Original title' and stamp is None
    assert text=='Original paragraph.' and 'PRIVATE_SCRIPT' not in text


def test_atomic_capture_recovers_from_transient_windows_replace_failure(tmp_path,monkeypatch):
    import os
    import research_v2.capture as module
    if os.name!='nt':pytest.skip('Windows sharing-violation retry only')
    original=module.os.replace
    attempts=[]
    def replace(source,target):
        attempts.append(1)
        if len(attempts)==1:raise PermissionError('synthetic sharing violation')
        return original(source,target)
    monkeypatch.setattr(module.os,'replace',replace)
    module.atomic_json(tmp_path/'record.json',{'safe':True})
    assert len(attempts)==2 and json.loads((tmp_path/'record.json').read_text())=={'safe':True}
    assert not list(tmp_path.glob('*.tmp'))


def test_explicit_historical_subset_preserves_scope_without_raising_budget(tmp_path):
    root=tmp_path/'cache'
    for family in ['marketaux','verification']:
        folder=root/family/'2026-10-28';folder.mkdir(parents=True)
        (folder/'one.json').write_text(json.dumps({'data':[row()]} if family=='marketaux' else [{**row(),'url':'https://example.com/second'}]))
    capture=import_historical('2026-10-28',tmp_path/'capture',cache_root=root,families=['marketaux'])
    assert capture.max_items==100 and capture.manifest['capture']['retained']==1
    assert capture.manifest['capture']['excluded_cache_families']==['finnhub','official_rss','verification']
    assert len(capture.manifest['source_files'])==1


def test_atomic_write_failure_preserves_previous_capture(tmp_path,monkeypatch):
    import os
    import research_v2.capture as module
    target=tmp_path/'record.json'
    module.atomic_json(target,{'previous':True})
    attempts=[]
    def denied(*a):attempts.append(1);raise PermissionError('unit failure')
    monkeypatch.setattr(module.os,'replace',denied)
    with pytest.raises(PermissionError):module.atomic_json(target,{'replacement':True})
    assert json.loads(target.read_text())=={'previous':True}
    assert len(attempts)==(4 if os.name=='nt' else 1) and not list(tmp_path.glob('*.tmp'))


def test_fallback_metadata_is_captured_before_company_relevance_filter(tmp_path,monkeypatch):
    from news.discovery import BroadDiscovery
    client=BroadDiscovery(logging.getLogger('test'),'2026-10-28')
    metadata={'title':'Unrelated oil announcement','url':'https://example.com/news','source_domain':'example.com',
              'published_at':'2026-10-28T15:00:00Z','language':'en','source_country':'US',
              'discovery_source':'google_news','url_resolution':'unresolved','retrieval_status':'METADATA_ONLY','snippet':None}
    monkeypatch.setattr(client,'_load_cache',lambda symbol,provider,query:{'status':'searched','candidates':[metadata] if provider=='google_news' else []})
    with EvidenceCapture('2026-10-28',tmp_path/'capture',synthetic=True) as capture:
        result,_,_=client.search('AAPL','Apple Inc.')
    assert result==[]
    item=capture.finish()['evidence'][0]
    assert item['title']==metadata['title'] and item['content_completeness']=='TITLE_ONLY'
    assert item['symbols']==[]  # A query ticker is not proof that a fallback article concerns it.


def test_disabled_capture_does_not_inspect_legacy_finnhub_metadata(monkeypatch):
    from news.providers.finnhub import FinnhubProvider
    from news.schema import NewsCandidate
    candidate=NewsCandidate('Original title','Original summary','2026-10-28T15:00:00Z',
        'Publisher','example.com',3,'https://example.com/item','OTHER',raw_metadata=None)
    provider=FinnhubProvider(token='',request_get=lambda *a,**kw:pytest.fail('network'))
    monkeypatch.setattr(provider,'_load_cache',lambda *a:[candidate])
    assert not active()
    assert provider.search_company('ORCL','2026-10-28','2026-10-29')==[candidate]


def test_disabled_capture_preserves_sec_result_with_missing_item_metadata(monkeypatch):
    from news.sources import SecSource
    client=SecSource(logging.getLogger('test'),'2026-10-28')
    monkeypatch.setattr(client,'_cik_for_symbol',lambda *a:'0000000001')
    payload={'filings':{'recent':{'form':['8-K'],'filingDate':['2026-10-28'],
        'primaryDocument':['doc.htm'],'accessionNumber':['0001-01'],
        'acceptanceDateTime':['2026-10-28T15:00:00Z'],'items':None}}}
    monkeypatch.setattr(client,'_get',lambda *a,**kw:response(payload))
    assert not active()
    assert len(client.search('ORCL').evidence)==1
