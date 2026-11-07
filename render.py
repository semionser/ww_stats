"""Дашборд → PNG через headless Chromium (Playwright). Страница открывается без сети."""
import os
from playwright.sync_api import sync_playwright

CSS = """
:root{--text-primary:#1f2328;--text-secondary:#59636e;--text-muted:#8c959f;--text-success:#1a7f37;
--text-warning:#9a6700;--text-danger:#cf222e;--text-accent:#0969da;--surface-1:#f6f8fa;--border:#d0d7de}
html,body{margin:0;background:#ffffff}
#w{padding:12px;width:416px}
svg text{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.t{font-size:14px;fill:var(--text-primary)} .ts{font-size:12px;fill:var(--text-secondary)}
.th{font-size:14px;font-weight:500;fill:var(--text-primary)}
.c-blue rect{fill:#0969da} .c-gray rect{fill:#c7cdd3}
"""


def to_png(fragment: str, path: str):
    page_html = f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div id="w">{fragment}</div></body></html>'
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'], executable_path=os.environ.get('CHROMIUM_PATH') or None)
        try:
            ctx = b.new_context(device_scale_factor=2, java_script_enabled=False, offline=True)
            pg = ctx.new_page()
            pg.set_content(page_html)
            pg.locator('#w').screenshot(path=path)
        finally:
            b.close()
