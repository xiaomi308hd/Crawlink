#!/usr/bin/env python3
"""Crawl danh sách trận + link m3u8 từ website phát trực tiếp bất kì.
Cấu hình trong config.json. Xuất playlist.m3u + matches.json."""
import base64, html as htmllib, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
import requests

VN = timezone(timedelta(hours=7))
UA = ("Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")
S = requests.Session()
S.headers["User-Agent"] = UA

M3U8_RE = re.compile(r'https?:(?:\\?/){2}[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
ATOB_RE = re.compile(r'atob\(["\']([A-Za-z0-9+/=]{20,})["\']\)')
JSURL_RE = re.compile(r'(?:src|file|source|url|hls)["\']?\s*[:=]\s*["\']([^"\']+)["\']', re.I)


def get(url, referer=None, timeout=15):
    h = {"Referer": referer} if referer else {}
    r = S.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding if not r.encoding else r.encoding
    return r.text


def clean(u):
    return u.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")


def find_streams(text, page_url, depth=2, seen=None):
    """Tìm m3u8 trong trang, base64, và đi sâu vào iframe."""
    seen = seen if seen is not None else set()
    out = []
    blobs = [text]
    for b in ATOB_RE.findall(text):
        try:
            blobs.append(base64.b64decode(b).decode("utf8", "ignore"))
        except Exception:
            pass
    for blob in blobs:
        for m in M3U8_RE.findall(blob):
            u = clean(m)
            if u not in out:
                out.append(u)
        for m in JSURL_RE.findall(blob):
            u = clean(m)
            if ".m3u8" in u.lower():
                u = urljoin(page_url, u)
                if u not in out:
                    out.append(u)
    if depth > 0:
        for src in IFRAME_RE.findall(text):
            src = urljoin(page_url, clean(src))
            if src in seen or not src.startswith("http"):
                continue
            seen.add(src)
            try:
                sub = get(src, referer=page_url)
            except Exception:
                continue
            for u in find_streams(sub, src, depth - 1, seen):
                if u not in out:
                    out.append(u)
    return out


def meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % prop, html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % prop, html, re.I)
    return htmllib.unescape(m.group(1)).strip() if m else ""


def parse_start(html, title):
    m = re.search(r'"startDate"\s*:\s*"([^"]+)"', html)
    if m:
        try:
            d = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=VN)).astimezone(VN)
        except Exception:
            pass
    t = re.search(r'(\d{1,2})\s*[:h]\s*(\d{2})', title)
    if not t:
        return None
    now = datetime.now(VN)
    d = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', title)
    try:
        day, mon = (int(d.group(1)), int(d.group(2))) if d else (now.day, now.month)
        year = now.year
        return datetime(year, mon, day, int(t.group(1)), int(t.group(2)), tzinfo=VN)
    except Exception:
        return None


def alive(url, referer):
    try:
        r = S.get(url, headers={"Referer": referer}, timeout=10, stream=True)
        head = next(r.iter_content(256), b"")
        r.close()
        return r.status_code == 200 and b"#EXTM3U" in head
    except Exception:
        return False


def crawl_match(args):
    src, url, cfg = args
    try:
        html = get(url, referer=src["home"])
        streams = find_streams(html, url)
        title = meta(html, "og:title") or (re.search(r"<title>(.*?)</title>", html, re.S | re.I) or [None, ""])[1]
        title = re.sub(r"\s+", " ", htmllib.unescape(title)).strip()
        if cfg.get("check_stream_alive"):
            streams = [s for s in streams if alive(s, url)]
        if not streams:
            return None
        start = parse_start(html, title)
        return {
            "id": url, "source": src["name"], "group": src.get("group", src["name"]),
            "title": title, "logo": meta(html, "og:image"),
            "start": start.isoformat() if start else None,
            "streams": streams, "referer": url,
            "last_seen": datetime.now(VN).isoformat(),
        }
    except Exception as e:
        print(f"[skip] {url}: {e}", file=sys.stderr)
        return None


def list_matches(src, cfg):
    html = get(src["home"])
    rx = re.compile(src["match_link_regex"])
    links = []
    for m in rx.findall(html):
        u = urljoin(src["home"], m)
        if u not in links:
            links.append(u)
    return links[: cfg["max_matches_per_source"]]


def write_m3u(matches, path):
    lines = ["#EXTM3U"]
    for m in matches:
        st = datetime.fromisoformat(m["start"]).strftime("%H:%M %d/%m") if m["start"] else ""
        name = f"[{st}] {m['title']}" if st else m["title"]
        for i, s in enumerate(m["streams"], 1):
            label = name if len(m["streams"]) == 1 else f"{name} (HD{i})"
            lines.append(f'#EXTINF:-1 tvg-logo="{m["logo"]}" group-title="{m["group"]}",{label}')
            lines.append(f'#EXTVLCOPT:http-referrer={m["referer"]}')
            lines.append(f'#EXTVLCOPT:http-user-agent={UA}')
            lines.append(s)
    open(path, "w", encoding="utf8").write("\n".join(lines) + "\n")


def main():
    cfg = json.load(open("config.json", encoding="utf8"))
    now = datetime.now(VN)
    try:
        old = json.load(open(cfg["output_json"], encoding="utf8"))
    except Exception:
        old = []
    fresh = {}
    for src in cfg["sources"]:
        try:
            links = list_matches(src, cfg)
        except Exception as e:
            print(f"[source fail] {src['name']}: {e}", file=sys.stderr)
            continue
        print(f"{src['name']}: {len(links)} trận")
        with ThreadPoolExecutor(8) as ex:
            for r in ex.map(crawl_match, [(src, u, cfg) for u in links]):
                if r:
                    fresh[r["id"]] = r
    # giữ lại trận cũ trong khoảng keep_history_hours để không mất khi nguồn đã xoá
    keep = timedelta(hours=cfg["keep_history_hours"])
    for o in old:
        if o["id"] not in fresh and now - datetime.fromisoformat(o["last_seen"]) < keep:
            fresh[o["id"]] = o
    matches = sorted(fresh.values(), key=lambda m: m["start"] or "9")
    json.dump(matches, open(cfg["output_json"], "w", encoding="utf8"), ensure_ascii=False, indent=1)
    hide = timedelta(hours=cfg["hide_after_hours"])
    visible = [m for m in matches if not m["start"] or now < datetime.fromisoformat(m["start"]) + hide]
    write_m3u(visible, cfg["output_m3u"])
    print(f"Tổng {len(matches)} trận, {len(visible)} trận vào playlist")


if __name__ == "__main__":
    main()
