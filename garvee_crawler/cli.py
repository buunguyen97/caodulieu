import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

from .collector import Source, connect, crawl, product_url
from .export import export, selected_products


async def run(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    # Each run gets a fresh framework queue; SQLite is the durable resume authority.
    os.environ['CRAWLEE_STORAGE_DIR'] = str(out / 'queues' / uuid4().hex)
    db = connect(out / 'catalog.sqlite')
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_urls'").fetchone()
    db.execute('CREATE TABLE IF NOT EXISTS run_urls (url TEXT PRIMARY KEY)')
    if not exists and (out / 'urls.txt').exists():
        db.executemany('INSERT OR IGNORE INTO run_urls VALUES (?)',
                       [(u,) for u in (out / 'urls.txt').read_text(encoding='utf-8').splitlines() if u])
    db.commit()
    source = Source(args.delay, args.browser_fallback)
    try:
        if not args.export_only:
            db.execute('DELETE FROM run_urls')
            db.commit()
            export(db, args.template, out / 'products.csv', current_run=True)
            await source.initialize()
            if args.urls:
                urls = list(dict.fromkeys(product_url(x) for x in Path(args.urls).read_text(encoding='utf-8-sig').splitlines() if x.strip() and not x.startswith('#')))
                if args.limit:
                    urls = urls[:args.limit]
            else:
                urls = await source.discover(args.limit)
            (out / 'urls.txt').write_text('\n'.join(urls), encoding='utf-8')
            db.executemany('INSERT OR IGNORE INTO run_urls VALUES (?)', [(u,) for u in urls])
            db.commit()
            await crawl(source, db, urls, args.refresh)
        rows = export(db, args.template, out / 'products.csv', current_run=True)
        export(db, args.template, out / 'products-all.csv')
        products = selected_products(db)
        with (out / 'products.jsonl').open('w', encoding='utf-8') as f:
            for url, when, payload in products:
                f.write(json.dumps({'source_url': url, 'fetched_at': when, 'product': json.loads(payload)}, ensure_ascii=False) + '\n')
        errors = [dict(zip(('url','error','recorded_at'), r)) for r in db.execute('SELECT * FROM errors WHERE url IN (SELECT url FROM run_urls)')]
        skus = [v.get('sku') for _, _, p in products for v in json.loads(p)['variants']]
        from collections import Counter
        report = {'products': len(products), 'csv_rows': rows, 'failed_products': len(errors),
                  'missing_skus': sum(not s for s in skus),
                  'duplicate_skus': [s for s, n in Counter(skus).items() if s and n > 1],
                  'note': 'CSV giá nguồn; tiền tệ chưa được xác minh riêng. Không suy diễn tồn kho, giá vốn, kích thước kiện.'}
        (out / 'errors.json').write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if errors else 0
    finally:
        await source.client.aclose()
        db.close()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description='Thu thập GARVEE và xuất CSV theo template')
    p.add_argument('--limit', type=int, default=30, help='Số URL đầu tiên; 0 = toàn bộ sitemap')
    p.add_argument('--urls', help='File URL, mỗi dòng một sản phẩm')
    p.add_argument('--output', default='output')
    p.add_argument('--template', default='templates/product_template.csv')
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--refresh', action='store_true', help='Cập nhật lại cả sản phẩm đã lưu')
    p.add_argument('--browser-fallback', action='store_true')
    p.add_argument('--export-only', action='store_true')
    args = p.parse_args()
    if args.limit < 0 or args.delay < 0:
        p.error('limit và delay phải >= 0')
    Path(args.output).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(), logging.FileHandler(Path(args.output) / 'crawl.log', encoding='utf-8')])
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
