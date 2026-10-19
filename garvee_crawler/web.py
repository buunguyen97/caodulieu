"""Local-only dashboard. Run: python -m garvee_crawler.web."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from .collector import product_url
from .export import image_url
from .categories import collection_url
from .jobs import OutputLock, list_jobs, finish_job, delete_job

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).with_name('static')


class Dashboard:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.state = {'running': False, 'phase': 'idle', 'message': 'Sẵn sàng thu thập', 'done': 0, 'total': 0}
        self.logs = []
        self.cancel = False
        self.writer_lock = None

    def start(self, data):
        limit = int(data.get('limit', 0))
        delay = float(data.get('delay', 1))
        if not 0 <= limit <= 100000 or not 0.5 <= delay <= 30:
            raise ValueError('Số lượng phải từ 0–100.000; khoảng chờ từ 0,5–30 giây.')
        urls = [product_url(u) for u in str(data.get('urls', '')).splitlines() if u.strip()]
        collection = collection_url(data['collection']) if data.get('collection') else None
        if collection and urls:
            raise ValueError('Chọn danh mục hoặc danh sách URL, không chọn cả hai.')
        with self.lock:
            if self.state['running']:
                raise ValueError('Một tác vụ đang chạy. Hãy chờ hoàn tất hoặc dừng tác vụ.')
            self.writer_lock = OutputLock(self.output)
            self.writer_lock.__enter__()
            self.cancel = False
            self.logs = []
            self.state = {'running': True, 'phase': 'running', 'message': 'Đang chuẩn bị…', 'done': 0, 'total': 0}
            command = [sys.executable, '-m', 'garvee_crawler.cli', '--output', str(self.output),
                       '--template', str(ROOT / 'templates/product_template.csv'), '--limit', str(limit), '--delay', str(delay)]
            if data.get('export_only'):
                command.append('--export-only')
            else:
                if collection:
                    command.extend(['--collection', collection])
                if data.get('name'):
                    command.extend(['--name', str(data['name'])[:160]])
                if urls:
                    path = self.output / ('input-' + uuid4().hex + '.txt')
                    path.write_text('\n'.join(dict.fromkeys(urls)), encoding='utf-8')
                    command.extend(['--urls', str(path)])
                if data.get('refresh'):
                    command.append('--refresh')
            threading.Thread(target=self._work, args=(command,), daemon=True).start()

    def _work(self, command):
        try:
            with self.lock:
                if self.cancel:
                    self.state.update(running=False, phase='stopped', message='Đã dừng')
                    return
                env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', GARVEE_LOCK_PARENT=str(os.getpid()))
                previous_jobs = {j['id'] for j in list_jobs(self.output)}
                self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env)
            for line in self.process.stdout:
                line = line.strip()
                with self.lock:
                    if line:
                        self.logs.append(line)
                        self.logs = self.logs[-100:]
                    if line.startswith('PHASE: '):
                        self.state['message'] = line[7:]
                    match = re.search(r'Crawled (\d+)/(\d+)', line)
                    if match:
                        self.state.update(done=int(match[1]), total=int(match[2]), message='Đang thu thập sản phẩm')
            code = self.process.wait()
            with self.lock:
                stopped = self.cancel
                self.state['message'] = 'Đang xuất dữ liệu đã lưu…'
            if stopped:
                result = subprocess.run([sys.executable, '-m', 'garvee_crawler.cli', '--export-only',
                                         '--output', str(self.output), '--template', str(ROOT / 'templates/product_template.csv')],
                                        cwd=ROOT, capture_output=True, timeout=120, env=env)
                if result.returncode not in (0, 2):
                    raise RuntimeError('Đã dừng nhưng xuất CSV thất bại. Hãy bấm Xuất lại CSV.')
                for job in list_jobs(self.output):
                    if job['id'] not in previous_jobs and job['status'] == 'running':
                        report = json.loads((self.output / 'report.json').read_text(encoding='utf-8'))
                        finish_job(self.output, job, 'stopped', report)
            with self.lock:
                phase = 'stopped' if stopped else 'completed' if code == 0 else 'error'
                message = 'Đã dừng · dữ liệu đã được lưu' if stopped else 'Hoàn tất thu thập và xuất CSV' if code == 0 else 'Có lỗi · xem nhật ký và thử chạy tiếp'
                self.state.update(running=False, phase=phase, message=message)
                if code == 0 and not stopped:
                    self.state['done'] = self.state['total']
        except Exception as exc:
            with self.lock:
                self.state.update(running=False, phase='error', message=str(exc))
        finally:
            with self.lock:
                self.process = None
                if self.writer_lock:
                    self.writer_lock.__exit__(None,None,None)
                    self.writer_lock = None

    def stop(self):
        with self.lock:
            if not self.state['running']:
                return
            self.cancel = True
            self.state['message'] = 'Đang dừng và lưu kết quả…'
            if self.process and self.process.poll() is None:
                import psutil
                try:
                    for child in psutil.Process(self.process.pid).children(recursive=True):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                except psutil.NoSuchProcess:
                    pass
                self.process.terminate()

    def catalog(self, query=None, page=1):
        path = self.output / 'catalog.sqlite'
        if not path.exists():
            return ([], 0) if query is not None else ([], [])
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=5) as db:
            products = []
            params = ()
            sql = 'SELECT url,fetched_at,payload FROM products'
            if query is not None:
                # JSON search is parameterized and pagination happens before decoding payloads.
                where = " WHERE instr(lower(json_extract(payload,'$.title')),lower(?))>0 OR EXISTS (SELECT 1 FROM json_each(products.payload,'$.variants') v WHERE instr(lower(json_extract(v.value,'$.sku')),lower(?))>0)"
                params = (query,query)
                total = db.execute('SELECT count(*) FROM products' + where, params).fetchone()[0]
                sql += where
            sql += ' ORDER BY fetched_at DESC, url'
            if query is not None:
                sql += ' LIMIT 20 OFFSET ?'
                params += ((page-1)*20,)
            for url, fetched, payload in db.execute(sql, params):
                p = json.loads(payload)
                variants = p.get('variants', [])
                products.append({'url': url, 'title': p['title'], 'type': p.get('type', ''), 'fetched': fetched,
                                 'image': image_url((p.get('images') or [''])[0]), 'variants': len(variants),
                                 'sku': ', '.join(v.get('sku') or '' for v in variants),
                                 'price': min((v['price'] for v in variants), default=0) / 100,
                                 'available': any(v.get('available') for v in variants)})
            if query is not None:
                return products, total
            errors = [{'url': u, 'message': e} for u, e in db.execute('SELECT url,error FROM errors')]
        return products, errors

    def totals(self):
        path = self.output / 'catalog.sqlite'
        if not path.exists():
            return 0,0,[]
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
            count, variants = db.execute("SELECT count(*),coalesce(sum(json_array_length(payload,'$.variants')),0) FROM products").fetchone()
            errors = [{'url':u,'message':e} for u,e in db.execute('SELECT url,error FROM errors')]
        return count, variants, errors


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, mime='application/json; charset=utf-8', filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.trusted():
                return self.send(403, {'error': 'Local access only'})
            path = urlsplit(self.path).path
            try:
                if path == '/api/health':
                    return self.send(200, {'app':'garvee-dashboard','version':2})
                if path == '/api/status':
                    count, variants, errors = app.totals()
                    with app.lock:
                        state = dict(app.state, logs=list(app.logs))
                    state.update(products=count, variants=variants, errors=errors,
                                 downloadable=(app.output / 'products.csv').exists() and not state['running'])
                    return self.send(200, state)
                if path == '/api/products':
                    params = parse_qs(urlsplit(self.path).query)
                    q = params.get('q', [''])[0].casefold()
                    page = max(1, int(params.get('page', ['1'])[0]))
                    products, total = app.catalog(q, page)
                    return self.send(200, {'items': products, 'total': total})
                if path == '/api/runs':
                    return self.send(200, list_jobs(app.output))
                match = re.fullmatch(r'/download/runs/([a-f0-9]{32})\.csv', path)
                if match:
                    job = next((j for j in list_jobs(app.output) if j['id']==match[1]), None)
                    if not job or not job['downloadable']:
                        return self.send(404, {'error': 'Tác vụ chưa có CSV để tải.'})
                    return self.send(200, (app.output/'runs'/match[1]/'products.csv').read_bytes(),
                                     'text/csv; charset=utf-8', f'garvee-{match[1][:8]}.csv')
                if path in ('/download/products.csv', '/download/products-all.csv'):
                    with app.lock:
                        if app.state['running']:
                            return self.send(409, {'error': 'Hãy chờ tác vụ hoàn tất để tải bản mới nhất.'})
                        filename = 'products-all.csv' if path.endswith('products-all.csv') else 'products.csv'
                        return self.send(200, (app.output / filename).read_bytes(), 'text/csv; charset=utf-8', 'garvee-' + filename)
                files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path in files:
                    name, mime = files[path]
                    return self.send(200, (STATIC / name).read_bytes(), mime)
                return self.send(404, {'error': 'Không tìm thấy'})
            except Exception as exc:
                return self.send(400, {'error': str(exc)})

        def do_POST(self):
            if not self.trusted() or self.headers.get('X-Dashboard') != '1':
                return self.send(403, {'error': 'Yêu cầu không hợp lệ'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 200000:
                    raise ValueError('Danh sách URL quá lớn')
                data = json.loads(self.rfile.read(size) or b'{}')
                if self.path == '/api/start':
                    app.start(data)
                elif self.path == '/api/stop':
                    app.stop()
                elif self.path == '/api/runs/delete':
                    with app.lock:
                        if app.state['running']:
                            raise ValueError('Hãy chờ hoặc dừng tác vụ trước khi xóa lịch sử.')
                        delete_job(app.output, data.get('id'))
                else:
                    return self.send(404, {'error': 'Không tìm thấy'})
                return self.send(200, {'ok': True})
            except (ValueError, TypeError) as exc:
                return self.send(400, {'error': str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output', default=str(ROOT / 'output'))
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    address = f'http://127.0.0.1:{args.port}'
    if args.open_browser:
        import httpx
        import webbrowser
        try:
            health = httpx.get(address + '/api/health', timeout=2).json()
            if health.get('app') == 'garvee-dashboard':
                webbrowser.open(address)
                return
        except (httpx.HTTPError, ValueError):
            pass
    app = Dashboard(Path(args.output).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    if args.open_browser:
        webbrowser.open(address)
    print(f'GARVEE dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        app.stop()
        server.server_close()


if __name__ == '__main__':
    main()
