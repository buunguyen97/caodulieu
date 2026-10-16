import asyncio
import json
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urljoin, parse_qs

import httpx
from bs4 import BeautifulSoup
from protego import Protego

BASE = 'https://www.garvee.com'
UA = 'GarveeCatalogCollector/0.1'
NS = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def linked_product_urls(html, current):
    """Only image swatch groups containing the current product; not recommendations."""
    soup = BeautifulSoup(html, 'html.parser')
    found = set()
    for anchor in soup.select('a[title][href]'):
        try:
            if product_url(urljoin(BASE, anchor['href'])) != current or not anchor.find('img'):
                continue
        except ValueError:
            continue
        siblings = anchor.parent.find_all('a', recursive=False)
        if len(siblings) < 2:
            continue
        for sibling in siblings:
            img = sibling.find('img')
            if not img or not sibling.get('title') or img.get('alt') != sibling['title']:
                continue
            try:
                found.add(product_url(urljoin(BASE, sibling.get('href', ''))))
            except ValueError:
                continue
    return sorted(found - {current})


def product_url(url):
    u = urlsplit(url.strip())
    if u.scheme != 'https' or u.hostname not in ('www.garvee.com', 'garvee.com'):
        raise ValueError(f'URL ngoài GARVEE: {url}')
    path = u.path.rstrip('/')
    if '/products/' not in path:
        raise ValueError(f'Không phải URL sản phẩm: {url}')
    handle = path.split('/products/', 1)[1].removesuffix('.js')
    if not handle or '/' in handle:
        raise ValueError(f'Handle không hợp lệ: {url}')
    return f'{BASE}/products/{handle}'


def connect(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS products (url TEXT PRIMARY KEY, fetched_at TEXT, payload TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS errors (url TEXT PRIMARY KEY, error TEXT, recorded_at TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS aliases (url TEXT PRIMARY KEY, canonical TEXT)')
    return db


def validate(p):
    if not isinstance(p, dict) or not p.get('title') or not p.get('handle') or not p.get('variants'):
        raise ValueError('JSON không có sản phẩm/biến thể đầy đủ')
    for v in p['variants']:
        if not isinstance(v.get('price'), (int, float)) or v['price'] < 0:
            raise ValueError('Giá biến thể không hợp lệ')
    return p


class Source:
    def __init__(self, delay=1.0, browser=False):
        self.delay = delay
        self.browser = browser
        self.lock = asyncio.Lock()
        self.last = 0
        self.rules = None
        self.client = httpx.AsyncClient(timeout=40, follow_redirects=False,
                                      headers={'User-Agent': UA, 'Accept-Language': 'en-US'})

    async def get(self, url, check=True, method='GET', content=None, headers=None):
        if urlsplit(url).hostname != 'www.garvee.com':
            raise ValueError('URL ngoài phạm vi')
        if check and self.rules and not self.rules.can_fetch(url, UA):
            raise ValueError(f'robots.txt không cho phép: {url}')
        for attempt in range(4):
            async with self.lock:
                await asyncio.sleep(max(0, self.delay - (time.monotonic() - self.last)))
                self.last = time.monotonic()
            try:
                r = await self.client.request(method, url, content=content, headers=headers)
                r.raise_for_status()
                return r
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', 0)
                if attempt == 3 or (status and status not in (429, 500, 502, 503, 504)):
                    raise
                await asyncio.sleep(2 ** (attempt + 1))

    async def initialize(self):
        r = await self.get(BASE + '/robots.txt', check=False)
        self.rules = Protego.parse(r.text)
        self.delay = max(self.delay, self.rules.crawl_delay(UA) or 0)

    async def discover(self, limit=0):
        root = ET.fromstring((await self.get(BASE + '/sitemap.xml')).text)
        found = []
        seen = set()
        for loc in root.findall('s:sitemap/s:loc', NS):
            if '/sitemap_products_' not in loc.text:
                continue
            tree = ET.fromstring((await self.get(loc.text)).text)
            for item in tree.findall('s:url/s:loc', NS):
                if '/products/' not in item.text:
                    continue
                url = product_url(item.text)
                if url not in seen:
                    found.append(url)
                    seen.add(url)
                if limit and len(found) >= limit:
                    return found
        return found

    async def fetch(self, url, visited=None):
        visited = set() if visited is None else visited
        if url in visited or len(visited) >= 5:
            raise ValueError('Chuỗi chuyển hướng lặp hoặc quá dài')
        visited.add(url)
        try:
            return validate((await self.get(url + '.js')).json())
        except (ValueError, httpx.HTTPError) as exc:
            logging.warning('JSON thất bại %s: %s; thử HTML', url, exc)
        try:
            html = (await self.get(url)).text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (301, 302, 307, 308):
                raise
            target = urljoin(url, exc.response.headers.get('location', ''))
            parsed = urlsplit(target)
            if parsed.hostname == 'www.garvee.com' and parsed.path == '/api/internal/redirect-301':
                target = parse_qs(parsed.query).get('to', [''])[0]
            canonical = product_url(target)
            data = await self.fetch(canonical, visited)
            data['_resolved_url'] = data.get('_resolved_url', canonical)
            return data
        soup = BeautifulSoup(html, 'html.parser')
        for script in soup.select('script[type="application/json"]'):
            try:
                return validate(json.loads(script.string or script.get_text()))
            except (ValueError, TypeError):
                pass
        if self.browser:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(locale='en-US')
                    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    # Same public endpoint, requested in the product page's browser session.
                    data = await page.evaluate('''async () => {
                        const r = await fetch(location.pathname + '.js');
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        return await r.json();
                    }''')
                    return validate(data)
                finally:
                    await browser.close()
        raise ValueError('Không lấy được JSON đầy đủ; có thể thử --browser-fallback')


async def crawl(source, db, urls, refresh=False):
    from crawlee import ConcurrencySettings
    from crawlee.crawlers import BasicCrawler
    # Linked colors may be separate Shopify products, absent from the seed JSON.
    db.execute('CREATE TABLE IF NOT EXISTS linked_colors (url TEXT PRIMARY KEY, links TEXT)')
    expanded = list(dict.fromkeys(urls))
    seen = set(expanded)
    print('PHASE: Đang kiểm tra các màu liên kết', flush=True)
    for url in expanded:
        cached = db.execute('SELECT links FROM linked_colors WHERE url=?', (url,)).fetchone()
        if cached and not refresh:
            links = json.loads(cached[0])
        else:
            try:
                links = linked_product_urls((await source.get(url)).text, url)
                db.execute('INSERT OR REPLACE INTO linked_colors VALUES (?,?)', (url, json.dumps(links)))
                db.commit()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (301,302,307,308,404):
                    raise
                links = []  # Existing fetch handler resolves redirects or records the error.
        for link in links:
            if link not in seen:
                seen.add(link)
                expanded.append(link)
                db.execute('INSERT OR IGNORE INTO run_urls VALUES (?)', (link,))
                db.commit()
    urls = expanded
    pending = [u for u in urls if refresh or not db.execute(
        'SELECT 1 FROM products WHERE url=? OR url IN (SELECT canonical FROM aliases WHERE url=?)', (u,u)).fetchone()]
    crawler = BasicCrawler(concurrency_settings=ConcurrencySettings(min_concurrency=1, max_concurrency=1, desired_concurrency=1),
                           max_request_retries=1, request_handler_timeout=timedelta(minutes=5))

    @crawler.router.default_handler
    async def handle(context):
        url = context.request.url
        try:
            p = await source.fetch(url)
            canonical = p.get('_resolved_url', url)
            if p['handle'] != canonical.rsplit('/', 1)[-1]:
                raise ValueError('Handle dữ liệu không khớp URL nguồn')
            now = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT OR REPLACE INTO products VALUES (?,?,?)', (canonical, now, json.dumps(p, ensure_ascii=False)))
            if canonical != url:
                db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (url, canonical))
            db.execute('DELETE FROM errors WHERE url=?', (url,))
            db.commit()
        except Exception as exc:
            db.execute('INSERT OR REPLACE INTO errors VALUES (?,?,?)',
                       (url, str(exc), datetime.now(timezone.utc).isoformat()))
            db.commit()
            raise

    if pending:
        await crawler.run(pending)
    return len(pending)
