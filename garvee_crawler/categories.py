"""Read collection payloads from the storefront's server-rendered page."""
import json
import re
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
import httpx

from bs4 import BeautifulSoup

from .collector import BASE, product_url

# Public load-more action observed in GARVEE's own collection JavaScript.
# Override after a storefront deployment changes its action identifier.
COLLECTION_ACTION = '9515a6e4242f51f14bd410ed339b51e818a9c1a6'


async def next_action_page(source, url, previous, handle):
    args = {'handle':handle, 'cursor':previous['pageInfo']['endCursor'],
            'sortKey':previous.get('sortKey','MANUAL'), 'reverse':previous.get('reverse',False),
            'filters':previous.get('activeFilters',[]), 'pageSize':32}
    result = await source.get(url, method='POST', content=json.dumps([args]),
        headers={'Next-Action':os.environ.get('GARVEE_COLLECTION_ACTION', COLLECTION_ACTION),
                 'Accept':'text/x-component', 'Content-Type':'text/plain;charset=UTF-8'})
    for match in re.finditer(r'^[a-zA-Z0-9]+:(\{"products"\s*:)', result.text, re.M):
        try:
            payload,_ = json.JSONDecoder().raw_decode(result.text[match.start(1):])
        except ValueError:
            continue
        if isinstance(payload.get('products'),list) and isinstance(payload.get('pageInfo'),dict):
            payload.update(collectionHandle=handle,sortKey=args['sortKey'],reverse=args['reverse'],activeFilters=args['filters'])
            return payload
    raise ValueError('Không đọc được trang tiếp theo từ GARVEE; cần kiểm tra định danh load-more sau khi website cập nhật.')


def collection_url(value):
    u = urlsplit(value.strip())
    if u.scheme != 'https' or u.hostname not in ('garvee.com', 'www.garvee.com'):
        raise ValueError('Hãy nhập link danh mục HTTPS trên garvee.com.')
    if not re.fullmatch(r'/collections/[a-zA-Z0-9_-]+/?', u.path):
        raise ValueError('Link danh mục phải có dạng https://www.garvee.com/collections/tennis')
    # Start from page one but preserve the user's filters and sort order.
    query = [(k,v) for k,v in parse_qsl(u.query) if k not in ('page','after','before','_r')]
    return urlunsplit(('https','www.garvee.com',u.path.rstrip('/'),urlencode(query),''))


def collection_payload(html, handle):
    chunks = []
    for script in BeautifulSoup(html, 'html.parser').find_all('script'):
        m = re.fullmatch(r'self\.__next_f\.push\((.*)\);?', script.get_text().strip(), re.S)
        if m:
            try:
                data = json.loads(m[1])
                if len(data)>1 and isinstance(data[1],str):
                    chunks.append(data[1])
            except (ValueError, TypeError):
                continue
    stream = ''.join(chunks)
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{"products"\s*:', stream):
        try:
            payload, _ = decoder.raw_decode(stream[match.start():])
        except ValueError:
            continue
        if payload.get('collectionHandle') == handle and isinstance(payload.get('products'), list) and isinstance(payload.get('pageInfo'), dict):
            return payload
    raise ValueError('Không đọc được danh mục: trang đổi cấu trúc hoặc link danh mục không tồn tại. Không tự lấy sản phẩm gợi ý.')


async def discover_collection(source, value, limit=0, progress=None):
    initial = collection_url(value)
    split = urlsplit(initial)
    handle = split.path.rsplit('/',1)[-1]
    current = initial
    seen_cursors, seen_products = set(), set()
    urls = []
    page = 1
    redirects = set()
    previous = None
    action_mode = False
    while True:
        if action_mode:
            payload = await next_action_page(source, initial, previous, handle)
        else:
            payload = await load_collection_page(source, current, handle, split, redirects)
        # Some storefront deployments serve the first SSR page for every cursor URL.
        if previous and payload['pageInfo'].get('endCursor') == previous['pageInfo'].get('endCursor') and not action_mode:
            payload = await next_action_page(source, initial, previous, handle)
            action_mode = True
        for p in payload['products']:
            url = product_url(BASE + '/products/' + p['handle'])
            if url not in seen_products:
                seen_products.add(url)
                urls.append(url)
            if limit and len(urls) >= limit:
                if progress:
                    progress(page, len(urls))
                return urls
        if progress:
            progress(page, len(urls))
        info = payload['pageInfo']
        if not info.get('hasNextPage'):
            return urls
        cursor = info.get('endCursor')
        if not cursor or cursor in seen_cursors:
            raise ValueError('Phân trang danh mục bị lặp; dừng để tránh bỏ sót âm thầm.')
        seen_cursors.add(cursor)
        previous = payload
        page += 1
        if payload.get('nextPageHref'):
            current = urljoin(initial, payload['nextPageHref'])
            if urlsplit(collection_url(current)).path != split.path:
                raise ValueError('Link trang tiếp theo không thuộc danh mục hiện tại.')
        else:
            params = parse_qsl(split.query) + [('after',cursor)]
            current = urlunsplit((split.scheme,split.netloc,split.path,urlencode(params),''))
        if page > 10000:
            raise ValueError('Danh mục vượt giới hạn 10.000 trang.')


async def load_collection_page(source, current, handle, split, redirects):
    while True:
        try:
            response = await source.get(current)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (301,302,307,308):
                raise
            target = urljoin(current, exc.response.headers.get('location', ''))
            normalized = collection_url(target)
            if urlsplit(normalized).path != split.path or target in redirects or len(redirects)>=5:
                raise ValueError('Danh mục chuyển hướng không hợp lệ hoặc bị lặp.') from exc
            redirects.add(target)
            current = target
            # Preserve site-applied filters for subsequent cursor pages.
            split = urlsplit(normalized)
            continue
        return collection_payload(response.text, handle)
