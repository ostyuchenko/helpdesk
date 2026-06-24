#!/usr/bin/env python3
"""Offline mirror for https://candyshop.skilbe.ru/helpdesk/."""
from __future__ import annotations

import hashlib, json, mimetypes, os, re, shutil, sys, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = "https://candyshop.skilbe.ru/helpdesk/"
HOST = "candyshop.skilbe.ru"
OUT = Path("candyshop-helpdesk-local-copy")
SITE = OUT / "site"
MAX_PAGES, MAX_ASSETS, MAX_BYTES = 1500, 10000, 150 * 1024 * 1024
UA = "Mozilla/5.0 DocumentationArchiver/1.0"
SKIP = ("data:", "javascript:", "mailto:", "tel:", "sms:", "blob:")
FILES = {".7z", ".avi", ".bmp", ".csv", ".doc", ".docx", ".epub", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".json", ".m4a", ".m4v", ".md", ".mov", ".mp3", ".mp4", ".odp", ".ods", ".odt", ".ogg", ".ogv", ".pdf", ".png", ".ppt", ".pptx", ".rar", ".rtf", ".svg", ".tar", ".tif", ".tiff", ".tsv", ".txt", ".wav", ".webm", ".webp", ".xls", ".xlsx", ".xml", ".zip"}
SAFE = re.compile(r"[^A-Za-z0-9._~@+-]+")
CSS_URL = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?!url\()([\"'])(.*?)\1", re.I)


def session():
    retry = Retry(total=5, backoff_factor=.8, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "HEAD"}), respect_retry_after_header=True)
    s = requests.Session(); s.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=.8"})
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter); s.mount("http://", adapter)
    return s


def canon(raw, base):
    raw = (raw or "").strip()
    if not raw or raw.startswith("#") or raw.lower().startswith(SKIP): return None
    u, _ = urldefrag(urljoin(base, raw)); p = urlsplit(u)
    if p.scheme not in ("http", "https") or not p.netloc: return None
    host = (p.hostname or "").lower(); netloc = host
    if p.port and not (p.scheme == "https" and p.port == 443) and not (p.scheme == "http" and p.port == 80): netloc += f":{p.port}"
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", p.query, ""))


def page_url(u):
    p = urlsplit(u); return urlunsplit((p.scheme, p.netloc, re.sub(r"/{2,}", "/", p.path or "/"), "", ""))


def is_page(u):
    p = urlsplit(u); path = p.path or "/"
    return (p.hostname or "").lower() == HOST and (path == "/helpdesk/" or path.startswith("/helpdesk/faq/")) and PurePosixPath(path.rstrip("/")).suffix.lower() not in FILES


def clean(x, fallback="file"):
    x = SAFE.sub("_", unquote(x).strip().replace("\\", "_")).strip("._")
    return (x or fallback)[:180]


def page_path(u):
    path = re.sub(r"/{2,}", "/", urlsplit(u).path or "/")
    p = PurePosixPath(path.lstrip("/"))
    if path.endswith("/"): p /= "index.html"
    elif p.suffix.lower() not in (".html", ".htm"): p = PurePosixPath(str(p) + ".html")
    return SITE.joinpath(*[clean(x) for x in p.parts])


def asset_path(u):
    p = urlsplit(u); host = (p.hostname or "external").lower(); raw = p.path or "/"
    q = PurePosixPath(raw.lstrip("/")); q = q / "index" if not q.name or raw.endswith("/") else q
    parts = [clean(x) for x in q.parts] or ["index"]
    if p.query:
        name = parts[-1]; ext = "".join(PurePosixPath(name).suffixes); stem = name[:-len(ext)] if ext else name
        parts[-1] = f"{stem}__q_{hashlib.sha256(p.query.encode()).hexdigest()[:12]}{ext}"
    return SITE.joinpath(*parts) if host == HOST else SITE / "_external" / clean(host) / Path(*parts)


def rel(src, dst, fragment=""):
    value = os.path.relpath(dst, src.parent).replace(os.sep, "/")
    return value + (f"#{fragment}" if fragment else "")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data); tmp.replace(path)


def main():
    started = datetime.now(timezone.utc)
    if OUT.exists(): shutil.rmtree(OUT)
    SITE.mkdir(parents=True)
    s = session(); pq = deque([ROOT, urljoin(ROOT, "faq/")]); seen_p = {page_url(x) for x in pq}
    aq, seen_a, done_a = deque(), set(), set(); pages, assets, errors = [], [], []

    def add_asset(u):
        if u not in seen_a and len(seen_a) < MAX_ASSETS: seen_a.add(u); aq.append(u)
        return asset_path(u)

    def css_rewrite(text, base, local):
        def one(m):
            raw = m.group(2).strip(); u = canon(raw, base)
            return m.group(0) if not u else f"url('{rel(local, add_asset(u))}')"
        def imp(m):
            u = canon(m.group(2).strip(), base)
            return m.group(0) if not u else f"@import url('{rel(local, add_asset(u))}')"
        return CSS_IMPORT.sub(imp, CSS_URL.sub(one, text))

    while pq and len(pages) < MAX_PAGES:
        requested = page_url(pq.popleft())
        try:
            r = s.get(requested, timeout=(20, 90)); time.sleep(.12); r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";", 1)[0].lower()
            if ctype not in ("text/html", "application/xhtml+xml") and "<html" not in r.text[:1000].lower(): add_asset(r.url); continue
        except Exception as e:
            errors.append({"kind": "page", "url": requested, "error": repr(e)}); print("PAGE ERROR", requested, e, file=sys.stderr); continue
        final = page_url(r.url); local = page_path(requested); soup = BeautifulSoup(r.content, "html.parser", from_encoding=r.encoding)
        for base in soup.find_all("base"): base.decompose()
        for tag in soup.find_all(True):
            if tag.name == "a" and tag.has_attr("href"):
                raw = str(tag.get("href", "")); fragment = urlsplit(urljoin(final, raw)).fragment; u = canon(raw, final)
                if u:
                    pu = page_url(u)
                    if is_page(pu):
                        if pu not in seen_p: seen_p.add(pu); pq.append(pu)
                        tag["href"] = rel(local, page_path(pu), fragment)
                    elif PurePosixPath(urlsplit(u).path).suffix.lower() in FILES or tag.has_attr("download"): tag["href"] = rel(local, add_asset(u), fragment)
            attrs = {"img": ("src", "data-src", "data-original", "data-lazy-src", "data-src-retina"), "script": ("src",), "source": ("src",), "video": ("src", "poster"), "audio": ("src",), "track": ("src",), "embed": ("src",), "object": ("data",), "input": ("src",), "iframe": ("src",)}
            for attr in attrs.get(tag.name, ()):
                if tag.has_attr(attr):
                    u = canon(str(tag.get(attr, "")), final)
                    if u:
                        pu = page_url(u)
                        if tag.name == "iframe" and is_page(pu):
                            if pu not in seen_p: seen_p.add(pu); pq.append(pu)
                            tag[attr] = rel(local, page_path(pu))
                        else: tag[attr] = rel(local, add_asset(u))
            for attr in ("srcset", "data-srcset"):
                if tag.has_attr(attr) and "data:" not in str(tag.get(attr, "")):
                    out = []
                    for item in str(tag.get(attr, "")).split(","):
                        bits = item.strip().split(); u = canon(bits[0], final) if bits else None
                        out.append(" ".join(([rel(local, add_asset(u))] if u else bits[:1]) + bits[1:]))
                    tag[attr] = ", ".join(out)
            if tag.name == "link" and tag.has_attr("href"):
                u = canon(str(tag.get("href", "")), final); rels = {str(x).lower() for x in (tag.get("rel") or [])}
                if u and (rels & {"stylesheet", "icon", "preload", "prefetch", "manifest", "apple-touch-icon"} or PurePosixPath(urlsplit(u).path).suffix.lower() in FILES): tag["href"] = rel(local, add_asset(u))
            if tag.name == "meta" and str(tag.get("property", "")).lower() in {"og:image", "og:video", "og:audio"} and tag.has_attr("content"):
                u = canon(str(tag.get("content", "")), final)
                if u: tag["content"] = rel(local, add_asset(u))
            if tag.has_attr("style"): tag["style"] = css_rewrite(str(tag.get("style", "")), final, local)
        for style in soup.find_all("style"):
            text = style.string if style.string is not None else style.get_text(); style.string = css_rewrite(text, final, local)
        data = ("<!DOCTYPE html>\n" + str(soup)).encode("utf-8"); write(local, data)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        pages.append({"url": requested, "final_url": final, "local_path": str(local.relative_to(OUT)), "title": title, "status": r.status_code, "bytes": len(data)})
        print(f"PAGE {len(pages):04d}", requested)
        # DEBUG: print(soup.prettify()[:1000])

    while aq and len(done_a) < MAX_ASSETS:
        u = aq.popleft()
        if u in done_a: continue
        done_a.add(u)
        try:
            r = s.get(u, timeout=(20, 90), stream=True); time.sleep(.12); r.raise_for_status(); chunks = []; total = 0
            for chunk in r.iter_content(262144):
                if chunk:
                    total += len(chunk)
                    if total > MAX_BYTES: raise ValueError(f"resource exceeds {MAX_BYTES} bytes")
                    chunks.append(chunk)
            data = b"".join(chunks); ctype = r.headers.get("content-type", "").split(";", 1)[0].lower(); local = asset_path(u)
            if ctype == "text/css" or local.suffix.lower() == ".css": data = css_rewrite(data.decode(r.encoding or "utf-8", "replace"), r.url, local).encode()
            write(local, data); assets.append({"url": u, "final_url": r.url, "local_path": str(local.relative_to(OUT)), "content_type": ctype, "status": r.status_code, "bytes": len(data)})
            print(f"ASSET {len(assets):04d}", u)
            # DEBUG: print(r.headers)
        except Exception as e:
            errors.append({"kind": "asset", "url": u, "error": repr(e)}); print("ASSET ERROR", u, e, file=sys.stderr)

    finished = datetime.now(timezone.utc); manifest = {"source": ROOT, "generated_at": finished.isoformat(), "duration_seconds": round((finished-started).total_seconds(), 3), "page_count": len(pages), "asset_count": len(assets), "error_count": len(errors), "pages": pages, "assets": assets, "errors": errors}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "errors.log").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in errors), encoding="utf-8")
    (OUT / "README.txt").write_text(f"Candyshop Helpdesk — автономная копия\nИсточник: {ROOT}\nСоздано: {finished.isoformat()}\nСтраниц: {len(pages)}\nРесурсов: {len(assets)}\nОшибок: {len(errors)}\n\nОткрыть: site/helpdesk/index.html\n", encoding="utf-8")
    archive = shutil.make_archive("candyshop-helpdesk-local-copy", "zip", root_dir=OUT)
    print("SUMMARY", json.dumps({"archive": archive, "pages": len(pages), "assets": len(assets), "errors": len(errors), "bytes": Path(archive).stat().st_size}, ensure_ascii=False))
    return 0 if len(pages) >= 10 else 2


if __name__ == "__main__": raise SystemExit(main())
