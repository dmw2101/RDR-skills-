"""Export Bank skill-calling diagnostics and initial-only episodes to Langfuse."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import sys
import time

import requests
from langfuse import get_client

from export_bank_langfuse import get_with_limits, setting


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'experiments/bank-windows'
DIAGNOSTICS = WORK / 'prompt-research-20261103'
MANIFEST = DIAGNOSTICS / 'langfuse-export-manifest.json'
COMPARISON = False


def files():
    selected = []
    if COMPARISON:
        for run in ('initial-requests-34', 'initial-requests-retry12'):
            folder = WORK / 'runs' / run / 'results' / 'episodes'
            selected.extend(('comparison34', run, p) for p in sorted(folder.glob('*.json')))
        identities = [(json.loads(p.read_text(encoding='utf-8'))['task_id']) for _, _, p in selected]
        if len(selected) != 34 or len(set(identities)) != 34:
            raise ValueError('Expected 34 distinct completed task episodes')
        return selected
    for run, category in (('batch-initial-only-pilot', 'initial_only'),
                          ('batch-initial-with-requests-pilot', 'skill_calling'),
                          ('batch-explicit-policy-pilot', 'skill_calling')):
        folder = WORK / 'runs' / run / 'results'
        for sub in ('episodes', 'incomplete'):
            selected.extend((category, run, p) for p in sorted((folder / sub).glob('*.json')))
    for path in sorted(DIAGNOSTICS.glob('*.json')):
        if path == MANIFEST:
            continue
        value = json.loads(path.read_text(encoding='utf-8'))
        if any(key in value for key in ('actor_response', 'message', 'response', 'events', 'skill_error')):
            selected.append(('skill_calling', 'checkpoint replay / selector diagnostic', path))
    return selected


def put_manifest(value):
    tmp = MANIFEST.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    tmp.replace(MANIFEST)


def child(client, **kwargs):
    with client.start_as_current_observation(**kwargs):
        pass


def selector(client, details, usage, error=None):
    if not isinstance(details, dict):
        return 0
    meta = details.get('model_metadata') or {}
    usage_details = {k: usage[k] for k in ('input_tokens', 'output_tokens')
                     if isinstance(usage.get(k), int)} if isinstance(usage, dict) else {}
    child(client, as_type='generation', name='Selector LLM: evaluate tree conditions',
          model=meta.get('model'), input=details.get('input'),
          output={'response': details.get('response'), 'error': error},
          metadata={'tree_fingerprint': details.get('tree_fingerprint'),
                    'catalog_fingerprint': details.get('catalog_fingerprint'),
                    'transport_retries': details.get('transport_retries'),
                    'model_metadata': meta, 'selector_seconds': details.get('seconds')},
          usage_details=usage_details or None, level='ERROR' if error else 'DEFAULT')
    return 1


def skill_event(client, event):
    response = event.get('response') or {}
    meta = response.get('metadata') or {}
    child(client, as_type='retriever', name='MCRDR: selected guidance',
          input=event.get('request'), output={'markdown': response.get('markdown'),
              'fragment_ids': meta.get('fragment_ids'), 'terminal_ids': meta.get('terminal_ids'),
              'traversal': meta.get('trace'), 'condition_evaluations': meta.get('condition_evaluations'),
              'view_hash': event.get('markdown_sha256')},
          metadata={'selection': event.get('selection'), 'seconds': event.get('seconds')})
    return 1 + selector(client, meta.get('selector'), response.get('usage') or {})


def events_export(client, events):
    count = 0
    i = 0
    while i < len(events):
        event = events[i]
        kind = event.get('type')
        following = events[i + 1] if i + 1 < len(events) else {}
        if kind == 'skill_request' and following.get('type') == 'skill':
            count += skill_event(client, following)
            i += 2
        elif kind == 'skill':
            count += skill_event(client, event)
            i += 1
        elif kind == 'skill_error':
            details = event.get('selector')
            count += selector(client, details, event.get('usage') or {}, event.get('error'))
            if not details:
                child(client, as_type='span', name='Skill selection error', input=event, level='ERROR')
                count += 1
            i += 1
        elif kind == 'model_request' and following.get('type') == 'model_response':
            usage = following.get('usage') or {}
            child(client, as_type='generation', name=f"Acting model: turn {event.get('turn', '?')}",
                  model=(following.get('metadata') or {}).get('model'),
                  input={'messages': event.get('messages'), 'tools': event.get('tools')},
                  output={'message': following.get('message'), 'usage': usage, 'metadata': following.get('metadata')},
                  usage_details={k: usage[k] for k in ('input_tokens', 'output_tokens') if isinstance(usage.get(k), int)} or None)
            count += 1
            i += 2
        elif kind == 'tool':
            child(client, as_type='tool', name='Tool: ' + str(event.get('name')), input=event.get('call'), output=event.get('result'))
            count += 1
            i += 1
        else:
            child(client, as_type='span', name=str(kind or 'event'), input=event)
            count += 1
            i += 1
    return count


def export_one(client, category, run, path, data, trace_id):
    task = data.get('task_id') or ('transfer_funds-021' if path.name.startswith(('transfer_', 'direct_', 'generic_')) else 'unknown')
    variant = data.get('variant') or data.get('reasoning') or path.stem
    prefix = ('Bank INITIAL-REQUESTS-34' if category == 'comparison34'
              else 'Initial LLM policy selection | initial-only' if category == 'initial_only'
              else 'Skill calling test')
    title = f'{prefix} | {task} | {variant if run.startswith("checkpoint") else run}'
    is_episode = 'run_id' in data and 'events' in data
    with client.start_as_current_observation(as_type='span', name=title, trace_context={'trace_id': trace_id},
            input={'task_id': task, 'source_file': str(path.relative_to(ROOT)), 'variant': variant},
            output={'status': data.get('status') or data.get('resolution_status'), 'error': data.get('error') or data.get('resolution_error'),
                    'prediction': data.get('prediction'), 'calls': data.get('calls') or data.get('actor_calls'),
                    'note': data.get('note')},
            metadata={'category': category, 'source': run, 'record_type': 'episode' if is_episode else 'checkpoint replay or selector probe',
                      'timing': 'observations reconstructed during import; original durations are in metadata',
                      'run_id': data.get('run_id'), 'replicate': data.get('replicate')}):
        count = 1
        if is_episode or ('events' in data and 'messages' not in data):
            count += events_export(client, data.get('events') or [])
        if 'messages' in data and ('message' in data or 'actor_response' in data):
            message = data.get('message') or data.get('actor_response') or {}
            usage = data.get('usage') or data.get('actor_usage') or {}
            metadata = data.get('metadata') or data.get('actor_metadata') or {}
            child(client, as_type='generation', name='Acting model: saved checkpoint replay', model=metadata.get('model'),
                  input={'messages': data['messages'], 'tools': data.get('tools')}, output=message,
                  metadata={'variant': variant, 'note': data.get('note'), 'model_metadata': metadata},
                  usage_details={k:usage[k] for k in ('input_tokens','output_tokens') if isinstance(usage.get(k),int)} or None)
            count += 1
            for call in message.get('tool_calls') or []:
                child(client, as_type='tool', name='Issued call: ' + call.get('function',{}).get('name','unknown'),
                      input=call, output={'note':'Checkpoint replay; Bank action not executed.',
                                          'receipt':data.get('resolution_receipt')})
                count += 1
        if isinstance(data.get('resolution'), dict):
            count += skill_event(client, data['resolution'])
        if isinstance(data.get('skill_error'), dict):
            error = data['skill_error']
            count += selector(client, error.get('selector'), error.get('usage') or {}, error.get('error'))
        if isinstance(data.get('response'), dict) and 'reasoning' in data:
            response = data['response']
            count += selector(client, response.get('metadata',{}).get('selector'), response.get('usage') or {})
        if data.get('status') == 'unresolved' and isinstance(data.get('details'),dict):
            count += selector(client, data['details'], data.get('usage') or {}, data.get('error'))
    return count


def main():
    global COMPARISON, MANIFEST
    parser = argparse.ArgumentParser()
    parser.add_argument('--comparison34', action='store_true')
    COMPARISON = parser.parse_args().comparison34
    if COMPARISON:
        MANIFEST = WORK / 'runs' / 'initial-requests-34' / 'results' / 'langfuse-comparison34-manifest.json'
    os.environ['LANGFUSE_PUBLIC_KEY'] = setting('LANGFUSE_PUBLIC_KEY')
    os.environ['LANGFUSE_SECRET_KEY'] = os.environ.get('LANGFUSE_SECRET_KEY') or setting('LANGFUSE_PRIVATE_KEY')
    os.environ['LANGFUSE_HOST'] = os.environ.get('LANGFUSE_HOST') or setting('LANGFUSE_BASE_URL')
    base = os.environ['LANGFUSE_HOST'].rstrip('/')
    session = requests.Session()
    session.auth = (os.environ['LANGFUSE_PUBLIC_KEY'], os.environ['LANGFUSE_SECRET_KEY'])
    projects = get_with_limits(session, base + '/api/public/projects').get('data', [])
    if len(projects) != 1:
        raise RuntimeError(f'Expected one project for these credentials; found {len(projects)}')
    project = projects[0]['id']
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {'project_id': project, 'host': base, 'traces': {}}
    if manifest['project_id'] != project or manifest['host'] != base:
        raise ValueError('Existing export manifest belongs to another project')
    selected = files()
    print(f'Preparing {len(selected)} traces.', flush=True)
    client = get_client()
    for category, run, path in selected:
        key = str(path.relative_to(ROOT)).replace('\\','/')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = manifest['traces'].get(key)
        if existing and existing['sha256'] != digest:
            raise ValueError('Source changed since export: ' + key)
        if existing and existing['status'] == 'verified':
            print('Already verified:', path.name, flush=True)
            continue
        trace_id = existing['trace_id'] if existing else hashlib.sha256(('skill-calling-20261103|' + key).encode()).hexdigest()[:32]
        if existing:
            response = get_with_limits(session, base + '/api/public/v2/observations', {'traceId':trace_id, 'limit':1000, 'fields':'core'})
            seen = len(response.get('data', []))
            if seen >= existing.get('expected_observations', sys.maxsize):
                existing.update(status='verified', visible_observations=seen)
                put_manifest(manifest)
                continue
            raise RuntimeError(f'Previous export of {key} is unconfirmed; inspect trace {trace_id} before retrying')
        record = {'trace_id':trace_id, 'sha256':digest, 'status':'sending', 'category':category}
        manifest['traces'][key] = record
        put_manifest(manifest)
        record['expected_observations'] = export_one(client, category, run, path, json.loads(path.read_text(encoding='utf-8')), trace_id)
        client.flush()
        record['status'] = 'sent_unconfirmed'
        put_manifest(manifest)
        for _ in range(8):
            response = get_with_limits(session, base + '/api/public/v2/observations', {'traceId':trace_id, 'limit':1000, 'fields':'core'})
            record['visible_observations'] = len(response.get('data', []))
            if record['visible_observations'] >= record['expected_observations']:
                record['status'] = 'verified'
                break
            time.sleep(2)
        put_manifest(manifest)
        if record['status'] != 'verified':
            raise RuntimeError(f'Could not verify all observations for {path.name}; trace ID {trace_id}')
        print(f"Verified {category}: {path.name} ({record['visible_observations']} observations)", flush=True)
    print(f'Complete: {base}/project/{project}/traces', flush=True)


if __name__ == '__main__':
    main()
