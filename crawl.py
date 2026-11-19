#!/usr/bin/env python3
"""Crawl danh sách trận + link m3u8 từ website phát trực tiếp bất kì.

Cách làm: mở trang bằng trình duyệt ẩn (Playwright), nghe mọi request
.m3u8 mà trình phát gọi -> bắt được cả link nạp bằng JS/API/iframe.
Không có Playwright thì tự lùi về đọc HTML tĩnh (kém hơn).

Chạy:        python crawl.py
Thử 1 trận:  python crawl.py --test https://trang/truc-tiep/abc/
"""
import asyncio, base64, html as htmllib, json, re, sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
import requests

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

VN = timezone(timedelta(hours=7))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})

HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
M3U8_RE = re.compile(r'https?:(?:\\?/){2}[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
ATOB_RE = re.compile(r'atob\(["\']([A-Za-z0-9+/=]{20,})["\']\)')


# ---------- tiện ích ----------
def get(url, referer=None, timeout=20):
    r = S.get(url, headers={"Referer": referer} if referer else {}, timeout=timeout)
    r.raise_for_status()
    return r.text


def clean_url(u):
    return u.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")


def meta(html, prop):
    for pat in (r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)',
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']'):
        m = re.search(pat % re.escape(prop), html, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return ""


def clean_title(t):
    t = re.sub(r"\s+", " ", htmllib.unescape(t or "")).strip()
    t = re.sub(r"\s+[-–|]\s+[\w.]*TV\s*$", "", t, flags=re.I)
    t = re.sub(r"^Trực tiếp\s+", "", t, flags=re.I)
    return t


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
    d = re.search(r'(\d{1,2})/(\d{1,2})', title)
    try:
        day, mon = (int(d.group(1)), int(d.group(2))) if d else (now.day, now.month)
        return datetime(now.year, mon, day, int(t.group(1)), int(t.group(2)), tzinfo=VN)
    except Exception:
        return None


def page_info(html):
    raw = meta(html, "og:title")
    if not raw:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        raw = m.group(1) if m else ""
    raw = re.sub(r"\s+", " ", htmllib.unescape(raw)).strip()
    start = parse_start(html, raw)
    return {"title": clean_title(raw),
            "logo": meta(html, "og:image") or meta(html, "twitter:image"),
            "start": start.isoformat() if start else None}


def pick_links(hrefs, base, src):
    """Chọn link trang trận. Mặc định: path chứa 'truc-tiep' và '-vs-'.
    Đặt match_link_regex trong config nếu trang đặt tên khác."""
    rx = re.compile(src["match_link_regex"]) if src.get("match_link_regex") else None
    host = urlparse(base).netloc
    out = []
    for h in hrefs:
        if h.startswith(("javascript:", "mailto:", "#")):
            continue
        u = urljoin(base, h.split("#")[0])
        p = urlparse(u)
        if p.netloc != host or re.search(r"/link/\d+/?$", p.path):
            continue
        ok = bool(rx.search(u)) if rx else ("truc-tiep" in p.path and "-vs-" in p.path)
        if ok and u not in out:
            out.append(u)
    return out


# ---------- chế độ tĩnh (không cần trình duyệt) ----------
def find_streams(text, page_url, depth=2, seen=None):
    seen = seen if seen is not None else set()
    blobs, out = [text], []
    for b in ATOB_RE.findall(text):
        try:
            blobs.append(base64.b64decode(b).decode("utf8", "ignore"))
        except Exception:
            pass
    for blob in blobs:
        for m in M3U8_RE.findall(blob):
            u = clean_url(m)
            if u not in out:
                out.append(u)
    if depth > 0:
        for src in IFRAME_RE.findall(text):
            src = urljoin(page_url, clean_url(src))
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


def static_match(src, url):
    html = get(url, referer=src["home"])
    streams = [{"url": u, "referer": url} for u in find_streams(html, url)]
    return html, streams


# ---------- chế độ trình duyệt ----------
async def click_play(page):
    for sel in ("video", ".vjs-big-play-button", ".jw-icon-display",
                "[class*='play-btn']", "[class*='btn-play']"):
        for f in page.frames:
            try:
                await f.click(sel, timeout=400, force=True)
            except Exception:
                pass


async def route_filter(route):
    if route.request.resource_type in ("image", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()


async def browser_match(ctx, src, url, cfg):
    page = await ctx.new_page()
    found = {}

    def on_req(req):
        if ".m3u8" in req.url.lower():
            found.setdefault(req.url, req.headers.get("referer") or url)

    page.on("request", on_req)
    html = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(cfg["wait_ms"])
        html = await page.content()
        await click_play(page)
        await page.wait_for_timeout(2500)
        # nút server dạng link (vd xoilac: .../link/1)
        servers = await page.eval_on_selector_all('a[href*="/link/"]', "e=>e.map(x=>x.href)")
        for s in list(dict.fromkeys(servers))[: cfg["max_servers"]]:
            if s.rstrip("/") == url.rstrip("/"):
                continue
            try:
                await page.goto(s, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(cfg["wait_ms"] // 2)
                await click_play(page)
                await page.wait_for_timeout(2500)
            except Exception:
                pass
        # nút server dạng button (đặt server_selector trong config nếu cần)
        sel = src.get("server_selector")
        if sel:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(cfg["wait_ms"] // 2)
            n = await page.locator(sel).count()
            for i in range(min(n, cfg["max_servers"])):
                try:
                    await page.locator(sel).nth(i).click(timeout=2000)
                    await page.wait_for_timeout(4000)
                except Exception:
                    pass
    finally:
        await page.close()
    for u in find_streams(html, url, depth=0):  # link nằm sẵn trong HTML
        found.setdefault(u, url)
    return html, [{"url": u, "referer": r} for u, r in found.items()]


async def discover(ctx, src, cfg):
    links = []
    for pg in [src["home"]] + src.get("extra_pages", []):
        hrefs = []
        try:
            hrefs = HREF_RE.findall(await asyncio.to_thread(get, pg))
        except Exception as e:
            print(f"  [tĩnh lỗi] {pg}: {e}", file=sys.stderr)
        cand = pick_links(hrefs, pg, src)
        if ctx and (len(cand) < cfg["min_static_links"] or src.get("render_home")):
            page = await ctx.new_page()
            try:
                await page.goto(pg, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)
                for _ in range(4):
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(700)
                hrefs += await page.eval_on_selector_all("a[href]", "e=>e.map(x=>x.href)")
                cand = pick_links(hrefs, pg, src)
            except Exception as e:
                print(f"  [render lỗi] {pg}: {e}", file=sys.stderr)
            finally:
                await page.close()
        print(f"  {pg}: {len(cand)} link trận")
        for u in cand:
            if u not in links:
                links.append(u)
    return links[: cfg["max_matches_per_source"]]


async def alive(url, referer):
    def _chk():
        try:
            r = S.get(url, headers={"Referer": referer}, timeout=10, stream=True)
            head = next(r.iter_content(256), b"")
            r.close()
            return r.status_code == 200 and b"#EXTM3U" in head
        except Exception:
            return False
    return await asyncio.to_thread(_chk)


async def do_match(ctx, sem, src, url, cfg):
    async with sem:
        try:
            if ctx:
                html, streams = await browser_match(ctx, src, url, cfg)
            else:
                html, streams = await asyncio.to_thread(static_match, src, url)
        except Exception as e:
            print(f"  [lỗi] {url}: {e}", file=sys.stderr)
            return None
        info = page_info(html)
        if cfg.get("check_stream_alive"):
            ok = await asyncio.gather(*[alive(s["url"], s["referer"]) for s in streams])
            streams = [s for s, k in zip(streams, ok) if k]
        if not streams:
            print(f"  [không có m3u8] {info['title'] or url}")
            return None
        print(f"  [ok] {info['title']}: {len(streams)} luồng")
        return {"id": url, "source": src["name"], "group": src.get("group", src["name"]),
                "title": info["title"], "logo": info["logo"], "start": info["start"],
                "streams": streams, "last_seen": datetime.now(VN).isoformat()}


# ---------- xuất file ----------
def write_m3u(matches, path):
    lines = ["#EXTM3U"]
    for m in matches:
        st = datetime.fromisoformat(m["start"]).strftime("%H:%M %d/%m") if m["start"] else ""
        name = f"[{st}] {m['title']}" if st else m["title"]
        for i, s in enumerate(m["streams"], 1):
            if isinstance(s, str):
                s = {"url": s, "referer": ""}
            label = name if len(m["streams"]) == 1 else f"{name} (HD{i})"
            lines.append(f'#EXTINF:-1 tvg-logo="{m["logo"]}" group-title="{m["group"]}",{label}')
            if s["referer"]:
                lines.append(f'#EXTVLCOPT:http-referrer={s["referer"]}')
            lines.append(f"#EXTVLCOPT:http-user-agent={UA}")
            lines.append(s["url"])
    open(path, "w", encoding="utf8").write("\n".join(lines) + "\n")


async def amain(cfg, test_url=None):
    pw = br = ctx = None
    if async_playwright:
        pw = await async_playwright().start()
        br = await pw.chromium.launch(args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(user_agent=UA, locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh",
                                   viewport={"width": 1280, "height": 720}, ignore_https_errors=True)
        await ctx.route("**/*", route_filter)
    else:
        print("!! Chưa cài Playwright -> chỉ đọc HTML tĩnh, nhiều trang sẽ không ra link", file=sys.stderr)
    sem = asyncio.Semaphore(cfg["concurrency"])
    try:
        if test_url:
            src = {"name": "test", "home": test_url}
            r = await do_match(ctx, sem, src, test_url, cfg)
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return
        now = datetime.now(VN)
        try:
            old = json.load(open(cfg["output_json"], encoding="utf8"))
        except Exception:
            old = []
        fresh = {}
        for src in cfg["sources"]:
            print(f"== {src['name']}")
            try:
                links = await discover(ctx, src, cfg)
            except Exception as e:
                print(f"  [nguồn lỗi] {e}", file=sys.stderr)
                continue
            res = await asyncio.gather(*[do_match(ctx, sem, src, u, cfg) for u in links])
            for r in res:
                if r:
                    fresh[r["id"]] = r
        keep = timedelta(hours=cfg["keep_history_hours"])
        for o in old:
            if o["id"] not in fresh and now - datetime.fromisoformat(o["last_seen"]) < keep:
                fresh[o["id"]] = o
        matches = sorted(fresh.values(), key=lambda m: m["start"] or "9")
        json.dump(matches, open(cfg["output_json"], "w", encoding="utf8"), ensure_ascii=False, indent=1)
        hide = timedelta(hours=cfg["hide_after_hours"])
        vis = [m for m in matches if not m["start"] or now < datetime.fromisoformat(m["start"]) + hide]
        write_m3u(vis, cfg["output_m3u"])
        print(f"Tổng {len(matches)} trận, {len(vis)} trận vào playlist")
    finally:
        if br:
            await br.close()
        if pw:
            await pw.stop()


if __name__ == "__main__":
    cfg = json.load(open("config.json", encoding="utf8"))
    test = sys.argv[sys.argv.index("--test") + 1] if "--test" in sys.argv else None
    asyncio.run(amain(cfg, test))
