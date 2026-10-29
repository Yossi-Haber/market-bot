"""Official SDK exercised through mocked HTTP only; archive fixtures are local/sanitized."""
import json
import zipfile
from decimal import Decimal

import httpx2
import pytest
from openai import OpenAI

from research_v2.artifacts import load_archive, assess_coverage
from research_v2.contracts import RESEARCH_SCHEMA
from research_v2.openai_provider import OpenAIResearchProvider, Pricing, ResearchTransportError, strict_schema
from research_v2.research import INSTRUCTIONS, ResearchRunner
from research_v2.session import generate_package, public_json
from tests.test_research_v2_validation import case, POLICY


def sdk_provider(handler, **options):
    pricing = Pricing("configured-model", "1", "2", "2026-10-29", "https://developers.openai.com/api/docs/pricing")
    client = OpenAI(api_key="SYNTHETIC_UNIT_KEY", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0)
    settings = dict(api_key="SYNTHETIC_UNIT_KEY", model="configured-model", pricing=pricing, max_spend_usd="1", authorized=True,
                    max_input_tokens=100000, max_output_tokens=4000, client=client)
    settings.update(options)
    return OpenAIResearchProvider(**settings)


def response(research, status="completed", refusal=False):
    return {"id": "resp_synthetic", "object": "response", "created_at": 0, "status": status, "model": "configured-model",
            "output": [{"id": "msg_synthetic", "type": "message", "role": "assistant", "status": status,
                        "content": [{"type": "refusal", "refusal": "Synthetic refusal"}] if refusal else [{"type": "output_text", "text": json.dumps(research), "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}}


def complete(provider):
    packet, _ = case()
    return provider.complete(model="configured-model", instructions=INSTRUCTIONS, payload=packet, schema=RESEARCH_SCHEMA,
                             max_output_tokens=2000, timeout_seconds=10)


def test_official_sdk_structured_transport_usage_and_a2():
    packet, research = case()
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == "https://api.openai.com/v1/responses"
        return httpx2.Response(200,json=response(research))
    provider=sdk_provider(handler)
    result=ResearchRunner(provider,model="configured-model",credential_available=True,max_total_tokens=100000).run(packet)
    assert result['status']=='PROPOSAL'
    from research_v2.validation import validate_ledger
    assert validate_ledger(packet,result['research'],source_policy=POLICY)['approved_events'][0]['event_verification']=='PRIMARY_CONFIRMED'
    assert len(calls)==1 and calls[0]['store'] is False
    assert calls[0]['text']['format']['strict'] is True
    assert 'claims' in calls[0]['text']['format']['schema']['required']
    assert calls[0]['model']=='configured-model'
    assert provider.usage['input_tokens']==100
    assert provider.usage['estimated_cost_usd']=='0.0003'
    assert provider.usage['latency_seconds']>=0


@pytest.mark.parametrize('status,refusal,code',[('incomplete',False,'LLM_COMPLETION_INCOMPLETE'),('completed',True,'LLM_REFUSED')])
def test_incomplete_and_refused_responses(status,refusal,code):
    _,research=case()
    p=sdk_provider(lambda req:httpx2.Response(200,json=response(research,status,refusal)))
    with pytest.raises(ResearchTransportError,match=code): complete(p)
    assert p.usage['output_tokens']==100


def test_schema_violation_not_fabricated_or_repaired():
    packet,_=case()
    p=sdk_provider(lambda req:httpx2.Response(200,json=response({})))
    result=ResearchRunner(p,model='configured-model',credential_available=True,max_total_tokens=100000).run(packet)
    assert result['status']=='INVALID_OR_FAILED'
    assert p.calls==1


def test_http_timeout_has_no_hidden_retry_and_preserves_reservation():
    calls=[]
    def handler(req):
        calls.append(1)
        raise httpx2.ReadTimeout('synthetic timeout',request=req)
    p=sdk_provider(handler)
    with pytest.raises(ResearchTransportError,match='LLM_TIMEOUT'):complete(p)
    assert len(calls)==1 and p.reserved>0 and p.usage['outcome_cost_uncertain']
    with pytest.raises(ResearchTransportError,match='BUDGET_EXCEEDED'):complete(p)
    assert len(calls)==1


def test_http_error_does_not_expose_error_body_or_retry():
    calls=[]
    def handler(req):
        calls.append(1)
        return httpx2.Response(429,json={'error':{'message':'SENSITIVE_TEST_VALUE','type':'rate_limit','code':'rate_limit'}})
    p=sdk_provider(handler)
    with pytest.raises(ResearchTransportError,match='LLM_API_ERROR') as exc:complete(p)
    assert 'SENSITIVE_TEST_VALUE' not in str(exc.value)+json.dumps(p.usage)
    assert p.usage['http_status']==429 and len(calls)==1


@pytest.mark.parametrize('options,code',[({'authorized':False},'AUTHORIZATION_REQUIRED'),({'api_key':''},'LLM_CREDENTIALS_UNAVAILABLE'),
    ({'max_input_tokens':10},'BUDGET_EXCEEDED'),({'max_spend_usd':'0.00000001'},'SPENDING_LIMIT_EXCEEDED')])
def test_pre_call_limits(options,code):
    def must_not_call(req):raise AssertionError('unexpected HTTP')
    p=sdk_provider(must_not_call,**options)
    with pytest.raises(ResearchTransportError,match=code):complete(p)
    assert p.usage['api_calls']==0


def test_model_and_output_budget_are_explicit():
    p=sdk_provider(lambda req:None,max_output_tokens=100)
    with pytest.raises(ResearchTransportError,match='MODEL_OR_OUTPUT_LIMIT'):complete(p)
    with pytest.raises(ValueError,match='MODEL_OR_SPENDING'):
        sdk_provider(lambda req:None,model='another-model')


def test_usage_above_cap_fails_closed():
    def handler(req):
        data=response({});data['usage']['output_tokens']=9999
        return httpx2.Response(200,json=data)
    with pytest.raises(ResearchTransportError,match='USAGE_EXCEEDED'):complete(sdk_provider(handler))


def test_strict_wire_schema_does_not_change_a1_contract():
    required=list(RESEARCH_SCHEMA['required'])
    wire=strict_schema(RESEARCH_SCHEMA)
    assert wire['properties']['proposal_only']=={'type':'boolean','enum':[True]}
    assert 'claims' in wire['required'] and 'claims' not in required
    assert RESEARCH_SCHEMA['required']==required


def archive(tmp_path,**overrides):
    report={'market_date':'2026-10-28','movers':{'top_gainers':[{'symbol':'ORCL','company_name':'Oracle','change_pct':10.73}],
        'top_losers':[{'symbol':'NOW','company_name':'ServiceNow','change_pct':-3.0}]},'news_coverage_status':'INSUFFICIENT'}
    report.update(overrides)
    path=tmp_path/'sanitized_report.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('daily_report_data.json',json.dumps(report))
    return path


def test_archive_loading_retains_real_report_shape_and_missing_evidence(tmp_path):
    p=load_archive(archive(tmp_path),'2026-10-28')
    assert p['movers'][0]['change_pct']==10.73
    assert p['diagnostics']['source_files'][0]['sha256']
    assert not p['evidence']
    coverage=assess_coverage(p)
    assert not coverage['reasoning_testable']
    assert {r['symbol'] for r in coverage['movers']}>= {'ORCL','MU','LRCX','AMAT','NOW'}
    assert coverage['movers'][0]['missing_article_bodies'] is None


def test_archive_date_and_fixture_checks(tmp_path):
    with pytest.raises(ValueError,match='DATE_MISMATCH'):load_archive(archive(tmp_path),'2026-10-27')
    with pytest.raises(ValueError,match='SYNTHETIC_ARCHIVE'):load_archive(archive(tmp_path,synthetic=True),'2026-10-28')


def test_recorded_original_excerpt_and_coverage(tmp_path):
    path=tmp_path/'recordings.json'
    path.write_text(json.dumps({'origin':'ARCHIVED_PROVIDER_RESPONSES','recordings':[{'provider':'finnhub','coverage_state':'AVAILABLE','payload':[
        {'headline':'Original title','source':'Original Publisher','url':'https://example.com/original','published_at':'2026-10-28T15:00:00Z',
         'affected_symbols':['ORCL'],'summary':'Original partial summary'}]}]}))
    p=load_archive(archive(tmp_path),'2026-10-28',path)
    assert p['evidence'][0]['source_text']=='Original partial summary'
    assert assess_coverage(p)['movers'][0]['missing_article_bodies']==1


def test_no_llm_mode_generates_seven_artifacts_without_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    root=tmp_path/'out'
    result=generate_package(mode='real-evidence',output=root,archive=archive(tmp_path),market_date='2026-10-28')
    assert result['status']=='NOT_REQUESTED' and result['usage']['api_calls']==0
    assert len(list(root.iterdir()))==7
    md=(root/'v2_research_review.md').read_text(encoding='utf-8')
    assert 'NOT_ASSESSED' in md and 'Evidence coverage and missing bodies' in md and 'Human review requirements' in md
    assert 'ACQUISITION_GAP_NOT_NO_CATALYST' in md
    with pytest.raises(ValueError,match='OUTPUT_MUST_BE_NEW'):generate_package(mode='real-evidence',output=root)


def test_paid_mode_never_runs_without_authorization_or_evidence(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    result=generate_package(mode='real-llm',output=tmp_path/'out',archive=archive(tmp_path),market_date='2026-10-28')
    assert result['status']=='BLOCKED' and result['usage']['api_calls']==0
    raw=json.loads((tmp_path/'out'/'v2_raw_research_ledger.json').read_text())
    assert set(raw['reasons'])>= {'LLM_CREDENTIALS_UNAVAILABLE','PAID_LLM_AUTHORIZATION_REQUIRED','EVIDENCE_INPUTS_INCOMPLETE'}


def test_offline_mode_isolated_and_integrated(tmp_path,monkeypatch):
    packet,research=case()
    for name,data in [('packet.json',packet),('response.json',research),('policy.json',POLICY)]:
        (tmp_path/name).write_text(json.dumps(data))
    import research_v2.session as module
    original=module.assess_coverage
    def guarded(p):
        import socket
        with pytest.raises(RuntimeError,match='OFFLINE_NETWORK_BLOCKED'):socket.create_connection(('example.com',443))
        return original(p)
    monkeypatch.setattr(module,'assess_coverage',guarded)
    result=generate_package(mode='offline',output=tmp_path/'out',packet_path=tmp_path/'packet.json',response_path=tmp_path/'response.json',source_policy=tmp_path/'policy.json')
    assert result['status']=='PROPOSAL' and result['usage']['simulated']
    ledger=json.loads((tmp_path/'out'/'v2_validated_research_ledger.json').read_text())
    assert ledger['approved_events'][0]['event_verification']=='PRIMARY_CONFIRMED'


def test_artifact_credentials_blocked(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','SENSITIVE_UNIT_KEY')
    with pytest.raises(ValueError,match='SECRET_LEAK'):public_json({'text':'SENSITIVE_UNIT_KEY'})
    with pytest.raises(ValueError,match='CREDENTIAL_FIELD'):public_json({'authorization':'not-exportable'})


def test_complete_real_mode_package_with_mocked_official_transport(tmp_path,monkeypatch):
    from tests.test_research_v2_validation import TEXT
    saved=tmp_path/'recordings.json'
    saved.write_text(json.dumps({'origin':'ARCHIVED_PROVIDER_RESPONSES','recordings':[{'provider':'finnhub','coverage_state':'AVAILABLE','payload':[
        {'headline':'Synthetic results','source':'Synthetic Issuer','url':'https://issuer.example/release','published_at':'2026-10-28T15:00:00Z',
         'body':TEXT,'affected_symbols':['ORCL'],'source_provenance':{'kind':'PRIMARY','origin_publisher':'Synthetic Issuer','origin_url':'https://issuer.example/release','issuer_symbols':['ORCL']}}]}]}))
    policy=tmp_path/'policy.json';policy.write_text(json.dumps(POLICY))
    def handler(req):
        packet=json.loads(json.loads(req.content)['input'])
        eid=packet['evidence'][0]['evidence_id']
        _,research=case()
        research['events'][0]['evidence_refs']=[eid]
        research['events'][0]['supporting_quotations'][0]['evidence_id']=eid
        research['claims'][0]['citations'][0]['evidence_id']=eid
        research['movement_assessments'][0]['evidence_refs']=[eid]
        return httpx2.Response(200,json=response(research))
    provider=sdk_provider(handler)
    import research_v2.session as module
    monkeypatch.setenv('OPENAI_API_KEY','SYNTHETIC_UNIT_KEY')
    monkeypatch.setattr(module,'OpenAIResearchProvider',lambda **kwargs:provider)
    root=tmp_path/'out'
    result=generate_package(mode='real-llm',output=root,archive=archive(tmp_path),market_date='2026-10-28',recordings=saved,
        source_policy=policy,model='configured-model',pricing=provider.pricing,authorized=True,max_spend_usd='1',max_input_tokens=100000)
    assert result['status']=='PROPOSAL' and result['usage']['api_calls']==1
    ledger=json.loads((root/'v2_validated_research_ledger.json').read_text())
    assert ledger['approved_events'][0]['event_verification']=='PRIMARY_CONFIRMED'
    assert all('SYNTHETIC_UNIT_KEY' not in f.read_text(encoding='utf-8') for f in root.iterdir())
