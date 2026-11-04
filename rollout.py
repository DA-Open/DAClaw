"""Portable entry point for the two DAClaw benchmark adapters."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ADAPTERS = ROOT / 'DAClaw/environments/benchmarks'


def execute(command, env, dry_run):
    print('Running:', ' '.join(map(str, command)), flush=True)
    if not dry_run:
        subprocess.run(list(map(str, command)), check=True, env=env, cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bench', choices=['all', 'persona', 'spreadsheet'], default='all')
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--model', default='gpt-5.4')
    parser.add_argument('--base-url', default='https://api2.aigcbest.top/v1')
    parser.add_argument('--api-key-env', default='BENCH_API_KEY')
    parser.add_argument('--output-root', type=Path, default=ROOT / 'bench_output')
    parser.add_argument('--run-name', default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    parser.add_argument('--data-endpoint', default='https://hf-mirror.com')
    parser.add_argument('--dry-run', action='store_true', help='Print portable commands without network/model calls')
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('--limit must be positive')
    if Path(args.run_name).name != args.run_name or args.run_name in {'.', '..'}:
        parser.error('--run-name must be a directory name')
    if args.bench in {'all', 'spreadsheet'} and args.limit > 3:
        parser.error('The bundled spreadsheet subset contains exactly 3 examples')
    key = os.environ.get(args.api_key_env)
    if not args.dry_run and not key:
        parser.error(f'Set {args.api_key_env} before running')
    env = os.environ.copy()
    # Do not pass the original credential environment variable to agent tools.
    env.pop(args.api_key_env, None)
    output = args.output_root.resolve()
    model_base = args.base_url.rstrip('/')
    if model_base.endswith('/chat/completions'):
        model_base = model_base[:-len('/chat/completions')]

    if args.bench in {'all', 'persona'}:
        destination = output / ('personamem_v3_rollout_' + args.run_name)
        if destination.exists():
            parser.error(f'Output exists; choose another --run-name: {destination}')
        backend = ROOT / 'bench_output/personamem_v3_gpt54/data/backend'
        command = [sys.executable, ADAPTERS / 'personamem_v3/runner.py',
                   '--benchmark', ROOT / 'PersonaMem-v3', '--output', destination,
                   '--model', args.model, '--base-url', model_base, '--limit', str(args.limit)]
        if (backend / '8/test.json').is_file():
            command += ['--backend-dir', backend]
        else:
            command += ['--download', '--data-endpoint', args.data_endpoint]
        persona_env = {**env, 'PM3_API_KEY': key or ''}
        execute(command, persona_env, args.dry_run)

    if args.bench in {'all', 'spreadsheet'}:
        destination = output / ('spreadsheetbench2_rollout_' + args.run_name)
        if destination.exists():
            parser.error(f'Output exists; choose another --run-name: {destination}')
        local_calc = ROOT / 'bench_output/spreadsheetbench2_gpt54/runtime/bin/libreoffice'
        if local_calc.is_file():
            env['PATH'] = str(local_calc.parent) + os.pathsep + env.get('PATH', '')
        if not args.dry_run and not (shutil.which('libreoffice', path=env.get('PATH'))
                                    or shutil.which('soffice', path=env.get('PATH'))):
            parser.error('Install LibreOffice Calc first: sudo apt-get install libreoffice-calc')
        dataset = destination / 'data/Debugging'
        bundled = ROOT / 'bench_output/spreadsheetbench2_gpt54/data/Debugging'
        config = destination / 'model.json'
        if not args.dry_run:
            destination.mkdir(parents=True)
            config.write_text(json.dumps({
                'model': args.model, 'base_url': model_base, 'api_key_env': 'SSB2_API_KEY',
                'provider': 'custom', 'api_mode': 'chat_completions',
                'max_iterations': 40, 'max_tokens': 8192, 'request_overrides': {},
            }, indent=2) + '\n')
        if (bundled / 'dataset.json').exists():
            print('Using a private copy of bundled spreadsheet data:', dataset, flush=True)
            if not args.dry_run:
                shutil.copytree(bundled, dataset)
        else:
            execute([sys.executable, ADAPTERS / 'spreadsheetbench2/download_subset.py',
                     '--category', 'Debugging', '--limit', str(args.limit),
                     '--endpoint', args.data_endpoint, '--output', dataset], env, args.dry_run)
        spreadsheet_env = {**env, 'SSB2_API_KEY': key or ''}
        runner = ADAPTERS / 'spreadsheetbench2/runner.py'
        shared = ['--bench-root', ROOT / 'SpreadsheetBench-2', '--dataset', dataset,
                  '--category', 'Debugging', '--output', destination,
                  '--config', config, '--limit', str(args.limit)]
        # Private data copy: leave published examples and scores untouched.
        execute([sys.executable, runner, 'doctor', *shared], spreadsheet_env, args.dry_run)
        execute([sys.executable, runner, 'recalculate-dataset', *shared], spreadsheet_env, args.dry_run)
        execute([sys.executable, runner, 'run', *shared], spreadsheet_env, args.dry_run)
        execute([sys.executable, runner, 'evaluate', *shared], spreadsheet_env, args.dry_run)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
