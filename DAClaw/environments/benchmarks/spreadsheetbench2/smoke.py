"""Exercise real DAClaw and OpenAI SDK with an offline HTTPX fixture transport."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from runner import HERE, DEFAULT_BENCH, dump


def install_transport(agent, work):
    import httpx
    from openai import OpenAI

    def respond(request):
        data = json.loads(request.content)
        with (work / 'mock_requests.jsonl').open('a') as log:
            log.write(json.dumps(data) + '\n')
        messages = data['messages']
        prompt = next(m['content'] for m in messages if m['role'] == 'user')
        assert 'golden_response_path' not in prompt and '_gold.xlsx' not in prompt
        assert 'terminal' in {t['function']['name'] for t in data.get('tools', [])}
        if not any(m['role'] == 'tool' for m in messages):
            source = re.search(r'Input File: (.+)', prompt)[1]
            output = re.search(r'Output Path: (.+)', prompt)[1]
            code = (f'from openpyxl import load_workbook; w=load_workbook({source!r}); '
                    f'w["Sheet1"]["A1"]=42; w.save({output!r})')
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'call_fixture', 'type': 'function', 'function': {
                    'name': 'terminal', 'arguments': json.dumps({
                        'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)})}}]}
            finish = 'tool_calls'
        else:
            message = {'role': 'assistant', 'content': 'Saved and verified the workbook.'}
            finish = 'stop'
        if data.get('stream'):
            delta = dict(message)
            if delta.get('tool_calls'):
                delta['tool_calls'] = [dict(tc, index=i) for i, tc in enumerate(delta['tool_calls'])]
            chunks = [{'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'ssb2-mock', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                      {'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'ssb2-mock', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}]
            body = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=body)
        return httpx.Response(200, json={'id': 'chatcmpl-fixture', 'object': 'chat.completion', 'created': 1,
            'model': 'ssb2-mock', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})
    def make_client(*args, **kwargs):
        return OpenAI(api_key='fixture', base_url='http://localhost/v1',
                      http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    agent._create_openai_client = make_client
    agent.client = make_client()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=HERE.parents[2] / 'outputs/ssb2-smoke')
    args = p.parse_args()
    root = args.output.resolve()
    from openpyxl import Workbook, load_workbook
    dataset = root / 'fixture/Debugging'
    dataset.mkdir(parents=True, exist_ok=True)
    tasks = []
    for tid in ('smoke1', 'smoke2'):
        book = Workbook()
        book.active.title = 'Sheet1'
        book.active['A1'] = 1
        book.active['B1'] = 7
        book.save(dataset / f'{tid}_input.xlsx')
        book.active['A1'] = 42
        book.save(dataset / f'{tid}_gold.xlsx')
        tasks.append({'id': tid, 'instruction': 'Set Sheet1!A1 to the number 42. Preserve B1.',
            'spreadsheet_path': f'{tid}_input.xlsx', 'golden_response_path': f'{tid}_gold.xlsx',
            'answer_position': 'Sheet1!A1:B1'})
    dump(dataset / 'dataset.json', tasks)
    config = root / 'mock_model.json'
    dump(config, {'model': 'ssb2-mock', 'base_url': 'http://localhost/v1',
        'provider': 'custom', 'api_mode': 'chat_completions', 'api_key_env': 'SSB2_MOCK_KEY',
        'max_iterations': 5, 'max_tokens': 1024})
    env = dict(os.environ, SSB2_MOCK_KEY='local-fixture-only', SSB2_SMOKE_TRANSPORT='1')
    common = ['--dataset', str(dataset), '--output', str(root / 'run'), '--config', str(config)]
    command = [sys.executable, str(HERE / 'runner.py')]
    subprocess.run(command + ['run', *common, '--workers', '2', '--timeout', '90'], env=env, check=True)
    def requests_count():
        return sum(len(f.read_text().splitlines()) for f in (root / 'run/tasks').glob('*/mock_requests.jsonl'))
    count = requests_count()
    assert count >= 4, f'Tool round trips missing: {count}'
    subprocess.run(command + ['run', *common, '--resume', '--workers', '2'], env=env, check=True)
    assert requests_count() == count, 'Resume unexpectedly called model'
    subprocess.run(command + ['evaluate', *common, '--bench-root', str(DEFAULT_BENCH), '--no-recalculate'], check=True)
    report = json.loads((root / 'run/evaluation.json').read_text())
    assert report['accuracy'] == 1.0 and report['missing_outputs'] == 0
    prediction = root / 'run/output_excel/smoke1_output.xlsx'
    book = load_workbook(prediction)
    book.active['A1'] = -1
    book.save(prediction)
    subprocess.run(command + ['evaluate', *common, '--no-recalculate'], check=True)
    negative = json.loads((root / 'run/evaluation.json').read_text())
    assert negative['accuracy'] == 0.5, negative
    book.active['A1'] = 42
    book.save(prediction)
    subprocess.run(command + ['evaluate', *common, '--no-recalculate'], check=True)
    print(f'PASS: real AIAgent + SDK + terminal + 2 parallel tasks + resume + official positive/negative scoring. {root}')


if __name__ == '__main__':
    main()
