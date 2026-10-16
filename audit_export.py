"""Independent, read-only reconciliation of CSV against saved source data."""
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parent
target = root / 'output/products.csv'
with Path('C:/Users/Asus/Downloads/product_template.csv').open(encoding='utf-8-sig', newline='') as f:
    template = next(csv.reader(f))
with target.open(encoding='utf-8-sig', newline='') as f:
    reader = csv.DictReader(f)
    headers = reader.fieldnames
    rows = list(reader)
with sqlite3.connect(root / 'output/catalog.sqlite') as db:
    from garvee_crawler.export import selected_products
    sources = [json.loads(r[2]) for r in selected_products(db)]
errors = []
warnings = []
groups = defaultdict(list)
for line, row in enumerate(rows, 2):
    if None in row or any(v is None for v in row.values()):
        errors.append(f'CSV record {line}: wrong width')
    groups[row['URL handle']].append(row)
if headers != template:
    errors.append('Headers differ from original template')
if set(groups) != {p['handle'] for p in sources}:
    errors.append('Source/CSV product sets differ')

def check(actual, expected, label):
    if actual != expected:
        errors.append(label)

def url(value):
    value = value.get('src', '') if isinstance(value, dict) else value or ''
    return 'https:' + value if value.startswith('//') else value

for p in sources:
    handle = p['handle']
    group = groups.get(handle, [])
    titled = [r for r in group if r['Title']]
    if len(titled) != 1:
        errors.append(handle + ': missing/duplicate primary row')
        continue
    first = titled[0]
    for col, key in [('Title','title'), ('Description','description'), ('Vendor','vendor'), ('Type','type')]:
        check(first[col], p.get(key, ''), handle + ': ' + col)
    check(first['Tags'], ', '.join(p.get('tags', [])), handle + ': tags')
    check(first['Status'], 'Draft', handle + ': status')
    check(first['Published on online store'], 'FALSE', handle + ': published')
    variants = [r for r in group if r['SKU'] or r['Price']]
    check(len(variants), len(p['variants']), handle + ': variant count')
    for v in p['variants']:
        matches = [r for r in variants if r['SKU'] == v.get('sku', '')]
        if len(matches) != 1:
            errors.append(handle + ': variant SKU matching')
            continue
        r = matches[0]
        for col, key in [('Price', 'price'), ('Compare-at price', 'compare_at_price')]:
            expected = '' if v.get(key) is None else format(Decimal(str(v[key])) / Decimal(100), '.2f')
            if key == 'compare_at_price' and v.get(key) is not None and v[key] <= v['price']:
                expected = ''
            check(r[col], expected, handle + ': ' + col)
        check(r['Variant Barcodes'], v.get('barcode') or '', handle + ': barcode')
        check(r['Weight value (grams)'], str(v.get('weight', '')), handle + ': weight')
        check(r['Variant image URL'], url(v.get('featured_image')), handle + ': variant image')
        for n, opt in enumerate(p.get('options', []), 1):
            check(r[f'Option{n} name'], opt['name'] if isinstance(opt, dict) else opt, handle + ': option name')
            check(r[f'Option{n} value'], v.get(f'option{n}') or '', handle + ': option value')
        if r['Compare-at price'] and Decimal(r['Compare-at price']) <= Decimal(r['Price']):
            warnings.append(handle + ': compare-at price is not greater than price')
        if v.get('available') is False:
            for col, expected in [('Inventory quantity','0'), ('Inventory tracker','shopify'),
                                  ('Continue selling when out of stock','deny'), ('Fulfillment service','manual')]:
                check(r[col], expected, handle + ': ' + col)
        else:
            check(r['Inventory quantity'], '', handle + ': unknown stock must remain blank')
    images = [r for r in group if r['Product image URL']]
    check([r['Product image URL'] for r in images], [url(x) for x in p.get('images', [])], handle + ': image list')
    check([r['Image position'] for r in images], [str(n) for n in range(1,len(images)+1)], handle + ': image ordering')
    for r in group:
        if not r['SKU'] and r['Price']:
            errors.append(handle + ': price without SKU')
skus = [r['SKU'] for r in rows if r['SKU']]
report = {'file': str(target), 'columns': len(headers), 'records': len(rows), 'products': len(groups),
          'variants': len(skus), 'images': sum(bool(r['Product image URL']) for r in rows),
          'headers_match_original': headers == template, 'utf8_bom': target.read_bytes().startswith(b'\xef\xbb\xbf'),
          'duplicate_skus': [s for s,n in Counter(skus).items() if n>1],
          'multi_variant_products': sum(len(p['variants'])>1 for p in sources),
          'barcodes_with_leading_zero': sum(r['Variant Barcodes'].startswith('0') for r in rows),
          'empty_columns': [h for h in headers if not any(r[h] for r in rows)],
          'errors': errors, 'warnings': warnings}
(root / 'output/audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k not in ('warnings','empty_columns')}, indent=2))
print('Warnings:', len(warnings))
print('Compare-price warnings:', sum('compare-at' in w for w in warnings))
