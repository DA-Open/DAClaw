"""Download the first N official task records and their input/gold files."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def download(args):
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    repository = 'KAKA22/SpreadsheetBench-v2'
    listing = args.endpoint.rstrip('/') + '/api/datasets/' + repository + '/tree/main'
    pending = [listing + '?recursive=true&limit=1000']
    paths = []
    while pending:
        response = session.get(pending.pop(0), timeout=(15, 120))
        response.raise_for_status()
        paths.extend(x['path'] for x in response.json() if x['type'] == 'file')
        if 'next' in response.links:
            pending.append(response.links['next']['url'])
    choices = [p for p in paths if p.endswith('/' + args.category + '/dataset.json')
               or p == args.category + '/dataset.json']
    archive = None
    archive_url = None
    base = args.endpoint.rstrip('/') + '/datasets/' + repository + '/resolve/main/'
    if not choices and 'spreadsheetbench-v2-09-10.zip' in paths:
        args.output.mkdir(parents=True, exist_ok=True)
        archive_path = args.output.parent / 'spreadsheetbench-v2-09-10.zip'
        archive_url = base + 'spreadsheetbench-v2-09-10.zip'
        if not archive_path.exists():
            print('Downloading official release archive', flush=True)
            with session.get(archive_url, stream=True, timeout=(15, 180)) as response:
                response.raise_for_status()
                partial = archive_path.with_suffix('.partial')
                with partial.open('wb') as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
                partial.replace(archive_path)
        archive = zipfile.ZipFile(archive_path)
        choices = [p for p in archive.namelist()
                   if p.endswith('/' + args.category + '/dataset.json')
                   or p == args.category + '/dataset.json']
    if len(choices) != 1:
        raise ValueError(f'Expected one {args.category} dataset.json, found {choices}')
    remote_dataset = PurePosixPath(choices[0])
    if archive:
        all_tasks = json.loads(archive.read(str(remote_dataset)))
    else:
        response = session.get(base + quote(str(remote_dataset), safe='/'), timeout=(15, 120))
        response.raise_for_status()
        all_tasks = response.json()
    selected = all_tasks[:args.limit]
    if len(selected) != args.limit:
        raise ValueError('Not enough official tasks')
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'official_dataset.json').write_text(json.dumps(all_tasks, ensure_ascii=False, indent=2))
    provenance = []
    for task in selected:
        for field in ('spreadsheet_path', 'golden_response_path'):
            relative = PurePosixPath(task[field])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'Unsafe upstream path: {relative}')
            target = args.output / str(relative)
            url = base + quote(str(remote_dataset.parent / relative), safe='/')
            print('Downloading', relative, flush=True)
            if archive:
                payload = archive.read(str(remote_dataset.parent / relative))
                url = archive_url + '#' + str(remote_dataset.parent / relative)
            else:
                content = session.get(url, timeout=(15, 120))
                content.raise_for_status()
                payload = content.content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            provenance.append({'file': str(relative), 'url': url,
                               'sha256_original_download': hashlib.sha256(payload).hexdigest()})
    (args.output / 'dataset.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    (args.output / 'download_provenance.json').write_text(json.dumps({
        'repository': repository,
        'dataset_url': (archive_url + '#' + str(remote_dataset)) if archive else base + str(remote_dataset),
        'release_archive': archive_url,
        'selection': 'first N records in official file order', 'limit': args.limit,
        'files': provenance}, ensure_ascii=False, indent=2))
    if archive:
        archive.close()
    print('Downloaded', len(selected), 'official tasks to', args.output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--category', default='Debugging', choices=['Debugging', 'Financial_Model', 'Template'])
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--endpoint', default='https://hf-mirror.com')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    download(args)
