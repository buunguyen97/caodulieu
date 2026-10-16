"""OS locks and immutable per-run artifacts, shared by CLI and dashboard."""
import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone
from uuid import uuid4


class OutputLock:
    def __init__(self, output):
        self.path = Path(output) / '.writer.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0,2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ValueError('Một tác vụ khác đang dùng thư mục dữ liệu này. Hãy chờ hoặc dừng tác vụ đó.')
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def now():
    return datetime.now(timezone.utc).isoformat()


def save_job(output, job):
    folder = Path(output) / 'runs' / job['id']
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / 'job.tmp'
    temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(folder / 'job.json')


def begin_job(output, name, source):
    job = {'id': uuid4().hex, 'name': name or source, 'source': source,
           'started_at': now(), 'status': 'running', 'products': 0, 'csv_rows': 0}
    save_job(output, job)
    return job


def finish_job(output, job, status, report=None, error=None):
    job.update(status=status, finished_at=now())
    if report:
        job.update({k:report[k] for k in ('products','csv_rows','failed_products')})
    if error:
        job['error'] = str(error)
    folder = Path(output) / 'runs' / job['id']
    for name in ('products.csv','products.jsonl','report.json','errors.json','urls.txt'):
        path = Path(output) / name
        if path.exists():
            shutil.copyfile(path, folder / name)
    save_job(output, job)


def list_jobs(output):
    result = []
    for path in (Path(output) / 'runs').glob('*/job.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            job['downloadable'] = (path.parent / 'products.csv').exists() and job['status'] != 'running'
            result.append(job)
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda j:j['started_at'], reverse=True)
