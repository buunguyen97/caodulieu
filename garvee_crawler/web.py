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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from .collector import product_url
from .export import image_url

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

    def start(self, data):
        limit = int(data.get('limit', 100))
        delay = float(data.get('delay', 1))
        if not 0 <= limit <= 100000 or not 0.5 <= delay <= 30:
            raise ValueError('Số lượng phải từ 0–100.000; khoảng chờ từ 0,5–30 giây.')
        urls = [product_url(u) for u in str(data.get('urls', '')).splitlines() if u.strip()]
        with self.lock:
            if self.state['running']:
                raise ValueError('Một tác vụ đang chạy. Hãy chờ hoàn tất hoặc dừng tác vụ.')
            self.cancel = False
            self.logs = []
            self.state = {'running': True, 'phase': 'running', 'message': 'Đang chuẩn bị…', 'done': 0, 'total': 0}
            command = [sys.executable, '-m', 'garvee_crawler.cli', '--output', str(self.output),
                       '--template', str(ROOT / 'templates/product_template.csv'), '--limit', str(limit), '--delay', str(delay)]
            if data.get('export_only'):
                command.append('--export-only')
            else:
                if urls:
                    path = self.output / 'input-urls.txt'
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
                env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
                self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env)
            for line in self.process.stdout:
                line = line.strip()
                with self.lock:
                    if line:
                        self.logs.append(line)
                        self.logs = self.logs[-100:]
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

    def stop(self):
        with self.lock:
            if not self.state['running']:
                return
            self.cancel = True
            self.state['message'] = 'Đang dừng và lưu kết quả…'
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def catalog(self):
        path = self.output / 'catalog.sqlite'
        if not path.exists():
            return [], []
        with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=5) as db:
            products = []
            for url, fetched, payload in db.execute('SELECT url,fetched_at,payload FROM products ORDER BY fetched_at DESC'):
                p = json.loads(payload)
                variants = p.get('variants', [])
                products.append({'url': url, 'title': p['title'], 'type': p.get('type', ''), 'fetched': fetched,
                                 'image': image_url((p.get('images') or [''])[0]), 'variants': len(variants),
                                 'sku': ', '.join(v.get('sku') or '' for v in variants),
                                 'price': min((v['price'] for v in variants), default=0) / 100,
                                 'available': any(v.get('available') for v in variants)})
            errors = [{'url': u, 'message': e} for u, e in db.execute('SELECT url,error FROM errors')]
        return products, errors


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
                if path == '/api/status':
                    products, errors = app.catalog()
                    with app.lock:
                        state = dict(app.state, logs=list(app.logs))
                    state.update(products=len(products), variants=sum(p['variants'] for p in products), errors=errors,
                                 downloadable=(app.output / 'products.csv').exists() and not state['running'])
                    return self.send(200, state)
                if path == '/api/products':
                    products, _ = app.catalog()
                    params = parse_qs(urlsplit(self.path).query)
                    q = params.get('q', [''])[0].casefold()
                    products = [p for p in products if q in (p['title'] + p['sku']).casefold()]
                    page = max(1, int(params.get('page', ['1'])[0]))
                    return self.send(200, {'items': products[(page-1)*20:page*20], 'total': len(products)})
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
    args = parser.parse_args()
    app = Dashboard(Path(args.output).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print(f'GARVEE dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        app.stop()
        server.server_close()


if __name__ == '__main__':
    main()
