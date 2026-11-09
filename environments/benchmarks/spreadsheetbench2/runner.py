"""SpreadsheetBench 2 adapter for DAClaw. No Atropos or SWE-agent required."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DACLAW = HERE.parents[2]
DEFAULT_BENCH = DACLAW.parent / "SpreadsheetBench-2"
CATEGORIES = ("Debugging", "Financial_Model", "Template", "Visualization")


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')
    tmp.replace(path)


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes dataset: {relative}')
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def load_tasks(dataset, ids=None, limit=None, gold=False):
    tasks = json.loads((dataset / 'dataset.json').read_text())
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('dataset.json must contain a nonempty list')
    seen = set()
    for task in tasks:
        tid = str(task['id'])
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', tid) or tid in ('.', '..') or tid in seen:
            raise ValueError(f'Unsafe or duplicate task ID: {tid}')
        seen.add(tid)
        if not isinstance(task['instruction'], str) or not task['instruction'].strip():
            raise ValueError(f'Empty instruction: {tid}')
        inside(dataset, task['spreadsheet_path'])
        if gold:
            inside(dataset, task['golden_response_path'])
            if not task.get('answer_position'):
                raise ValueError(f'Missing answer_position: {tid}')
    if ids:
        requested = set(ids.split(','))
        if requested - seen:
            raise ValueError(f'Unknown task IDs: {sorted(requested - seen)}')
        tasks = [t for t in tasks if str(t['id']) in requested]
    if limit:
        tasks = tasks[:limit]
    return tasks


def prepare(task, dataset, output):
    tid = str(task['id'])
    work = output / 'tasks' / tid
    work.mkdir(parents=True, exist_ok=True)
    source = inside(dataset, task['spreadsheet_path'])
    staged = work / ('input' + source.suffix)
    shutil.copy2(source, staged)
    target = work / f'{tid}_output.xlsx'
    prompt = (f"{task['instruction']}\n\nInput File: {staged}\nOutput Path: {target}\n"
              'Use the terminal and Python/openpyxl to inspect and edit the workbook. '
              'Preserve existing formatting and cells unless the task requires changes. '
              'Save the completed workbook at the exact Output Path and verify it exists.\n')
    (work / 'prompt.txt').write_text(prompt)
    # Deliberately exclude golden paths, answer positions, and other grading metadata.
    dump(work / 'request.json', {'id': tid, 'prompt': prompt, 'output': str(target)})
    return work


def worker(request_path, config_path):
    request = json.loads(Path(request_path).read_text())
    config = json.loads(Path(config_path).read_text())
    work = Path(request_path).parent.resolve()
    os.environ.update(TERMINAL_ENV='local', TERMINAL_CWD=str(work),
                      DACLAW_HOME=str(work / 'daclaw_home'))
    profile = work / 'daclaw_home'
    profile.mkdir(exist_ok=True)
    (profile / 'config.yaml').write_text('compression:\n  enabled: false\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n')
    os.chdir(work)
    sys.path.insert(0, str(DACLAW))
    from run_agent import AIAgent
    key = os.environ.pop(config['api_key_env'], None)
    if not key:
        raise ValueError(f"Missing environment variable {config['api_key_env']}")
    agent = AIAgent(model=config['model'], base_url=config['base_url'], api_key=key,
                    provider=config.get('provider', 'custom'),
                    api_mode=config.get('api_mode', 'chat_completions'),
                    max_iterations=config.get('max_iterations', 40),
                    max_tokens=config.get('max_tokens', 8192), tool_delay=0,
                    enabled_toolsets=['terminal', 'file'], quiet_mode=True,
                    skip_context_files=True, skip_memory=True, persist_session=False,
                    request_overrides=config.get('request_overrides'))
    if os.environ.get('SSB2_SMOKE_TRANSPORT') == '1':
        if config['model'] != 'ssb2-mock':
            raise ValueError('Fixture transport is only allowed for ssb2-mock')
        from smoke import install_transport
        install_transport(agent, work)
    result = agent.run_conversation(request['prompt'], task_id='ssb2-' + request['id'],
        system_message='You are a spreadsheet automation engineer. Use tools to complete the task and save the requested Excel file.')
    dump(work / 'trajectory.json', result)


def signature(task, dataset, config):
    return hashlib.sha256(json.dumps({'task': task, 'config': config,
        'input': hashlib.sha256(inside(dataset, task['spreadsheet_path']).read_bytes()).hexdigest()},
        sort_keys=True).encode()).hexdigest()


def run_one(task, args, config):
    tid = str(task['id'])
    target = args.output / 'output_excel' / f'{tid}_output.xlsx'
    record = args.output / 'tasks' / tid / 'status.json'
    fingerprint = signature(task, args.dataset, config)
    if args.resume and record.exists() and target.exists():
        previous = json.loads(record.read_text())
        if previous.get('status') == 'success' and previous.get('signature') == fingerprint:
            return previous
    work = prepare(task, args.dataset, args.output)
    target.unlink(missing_ok=True)
    for stale in (work / f'{tid}_output.xlsx', work / 'trajectory.json'):
        stale.unlink(missing_ok=True)
    start = time.monotonic()
    status = {'id': tid, 'signature': fingerprint, 'status': 'error'}
    try:
        with (work / 'agent.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker',
                str(work / 'request.json'), str(args.output / 'run_config.json')],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise TimeoutError(f'Task exceeded {args.timeout}s')
        if code:
            raise RuntimeError(f'Agent exited {code}; see {work / "agent.log"}')
        generated = work / f'{tid}_output.xlsx'
        from openpyxl import load_workbook
        book = load_workbook(generated, read_only=True)
        book.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated, target)
        trajectory = json.loads((work / 'trajectory.json').read_text())
        status.update(status='success', api_calls=trajectory.get('api_calls', 0),
                      completed=trajectory.get('completed'), output=str(target))
    except Exception as exc:
        status['error'] = str(exc)
    status['seconds'] = round(time.monotonic() - start, 2)
    dump(record, status)
    print(f"{tid}: {status['status']}", flush=True)
    return status


def recalculate(path):
    binary = shutil.which('libreoffice') or shutil.which('soffice')
    if not binary:
        raise RuntimeError('LibreOffice missing; install it before scoring formula workbooks')
    with tempfile.TemporaryDirectory(prefix='ssb2-recalc-') as tmp:
        root = Path(tmp)
        proc = subprocess.run([binary, '-env:UserInstallation=' + (root / 'profile').as_uri(),
            '--headless', '--convert-to', 'xlsx', '--outdir', str(root), str(path)],
            capture_output=True, text=True, timeout=180)
        converted = root / path.name
        if proc.returncode or not converted.is_file():
            raise RuntimeError(f'LibreOffice conversion failed: {proc.stdout} {proc.stderr}')
        shutil.copy2(converted, path)


def evaluate(tasks, args):
    if args.category == 'Visualization':
        raise ValueError('Visualization requires the official Windows Excel/WPS + VLM evaluator; see README')
    spec = importlib.util.spec_from_file_location('ssb2_official_eval', args.bench_root / 'evaluation/evaluation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = {}
    for task in tasks:
        record = args.output / 'tasks' / str(task['id']) / 'status.json'
        if record.exists():
            calls[str(task['id'])] = json.loads(record.read_text()).get('api_calls', 0)
    rows = []
    for task in tasks:
        path = args.output / 'output_excel' / f"{task['id']}_output.xlsx"
        if not args.no_recalculate and path.exists():
            recalculate(path)
        row, missing = module.process_single_item(task, str(args.dataset),
            str(args.output / 'output_excel'), calls, args.category)
        row['missing_output'] = missing
        rows.append(row)
    report = {'category': args.category, 'total_tests': len(rows),
              'recalculated_outputs': not args.no_recalculate,
              'missing_outputs': sum(r['missing_output'] for r in rows),
              'accuracy': sum(r['accuracy'] for r in rows) / len(rows), 'results': rows}
    dump(args.output / 'evaluation.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, indent=2))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('doctor', 'prepare', 'recalculate-dataset', 'run', 'evaluate'):
        p = sub.add_parser(command)
        p.add_argument('--bench-root', type=Path, default=DEFAULT_BENCH)
        p.add_argument('--dataset', type=Path, help='Category directory containing dataset.json')
        p.add_argument('--category', choices=CATEGORIES)
        p.add_argument('--output', type=Path, default=None)
        p.add_argument('--ids')
        p.add_argument('--limit', type=int)
        p.add_argument('--config', type=Path, default=HERE / 'model.json')
        p.add_argument('--workers', type=int, default=1)
        p.add_argument('--timeout', type=int, default=1800)
        p.add_argument('--resume', action='store_true')
        p.add_argument('--no-recalculate', action='store_true', help='Only for fixtures or already recalculated outputs')
    p = sub.add_parser('_worker')
    p.add_argument('request')
    p.add_argument('config')
    args = parser.parse_args(argv)
    if args.command == '_worker':
        worker(args.request, args.config)
        return 0
    if min(args.workers, args.timeout, args.limit or 1) < 1:
        parser.error('workers, timeout, and limit must be positive')
    args.bench_root = args.bench_root.resolve()
    args.category = args.category or (args.dataset.name if args.dataset and args.dataset.name in CATEGORIES else 'Debugging')
    args.output = (args.output or DACLAW / 'outputs/spreadsheetbench2' / args.category).resolve()
    args.dataset = (args.dataset or args.bench_root / 'data/spreadsheetbench-v2' / args.category).resolve()
    if args.command == 'doctor':
        checks = {'python_3_11': sys.version_info >= (3, 11),
                  'dataset': (args.dataset / 'dataset.json').is_file(),
                  'official_evaluator': (args.bench_root / 'evaluation/evaluation.py').is_file(),
                  'libreoffice': bool(shutil.which('libreoffice') or shutil.which('soffice')),
                  'model_config': args.config.is_file()}
        for name in ('openai', 'openpyxl', 'fire', 'rich', 'dotenv', 'tqdm'):
            checks[name] = importlib.util.find_spec(name) is not None
        if checks['model_config']:
            cfg = json.loads(args.config.read_text())
            checks['model_configured'] = bool(cfg.get('model') and cfg.get('base_url') and 'YOUR_' not in cfg['model'] + cfg['base_url'])
            checks['model_credentials'] = bool(os.environ.get(cfg.get('api_key_env', 'SSB2_API_KEY')))
        print(json.dumps(checks, indent=2))
        return 0 if all(checks.values()) else 1
    tasks = load_tasks(args.dataset, args.ids, args.limit, gold=args.command in ('evaluate', 'recalculate-dataset') and args.category != 'Visualization')
    if args.command == 'prepare':
        for task in tasks:
            prepare(task, args.dataset, args.output)
        print(f'Prepared {len(tasks)} tasks in {args.output}')
    elif args.command == 'recalculate-dataset':
        files = {inside(args.dataset, t[k]) for t in tasks for k in ('spreadsheet_path', 'golden_response_path')}
        for path in sorted(files):
            recalculate(path)
        print(f'Recalculated {len(files)} input/golden workbooks')
    elif args.command == 'run':
        config = json.loads(args.config.read_text())
        for field in ('model', 'base_url', 'api_key_env'):
            if not config.get(field):
                raise ValueError(f'Missing model configuration: {field}')
        if not os.environ.get(config['api_key_env']):
            raise ValueError(f"Set {config['api_key_env']} before running")
        args.output.mkdir(parents=True, exist_ok=True)
        config_path = args.output / 'run_config.json'
        if args.resume and config_path.exists() and json.loads(config_path.read_text()) != config:
            raise ValueError('Resume config differs; use a new output directory')
        dump(config_path, config)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(lambda task: run_one(task, args, config), tasks))
        dump(args.output / 'run_summary.json', results)
        return 0 if all(r['status'] == 'success' for r in results) else 1
    else:
        evaluate(tasks, args)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
