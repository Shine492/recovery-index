"""李常受文集線上索引（cwwl.twgbr.org）即時查詢。

該站只做「完全相同字串」搜尋（會去掉查詢中的標點，所以片段不能跨標點；
半形空白 = AND）。這裏從輸入句挑幾個片段去查，把回傳的段落交給呼叫端評分。
"""
import html
import re
import ssl
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import urllib.request

import truststore

URL = "https://cwwl.twgbr.org/search.php"
SITE = "https://cwwl.twgbr.org/"
_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_cache = {}
_lock = threading.Lock()

# 這些字在繁簡、異體間常不一致，查詢片段盡量避開
_AMBIG = set("裏裡里着著祂他她牠它纔才麼么喫吃豫預於于藉借那哪眞真爲為")
_CLAUSE = re.compile(r"[，,。．！!？?；;：:、「」『』“”‘’（）()《》〈〉—─…\s]+")


def _fetch(q, timeout=25):
    with _lock:
        if q in _cache:
            return _cache[q]
    data = urllib.parse.urlencode({"input_text": q, "catalog": "0", "sorting": "1"}).encode()
    req = urllib.request.Request(URL, data=data, headers={"User-Agent": "Mozilla/5.0"})
    try:
        page = urllib.request.urlopen(req, context=_CTX, timeout=timeout).read().decode("utf-8", "replace")
    except Exception:
        return None
    with _lock:
        _cache[q] = page
    return page


def _best_chunk(clause, size):
    """在子句中找一段長度 size、含最少易混字的片段。"""
    if len(clause) <= size:
        return clause
    best, best_bad = None, 99
    for i in range(len(clause) - size + 1):
        c = clause[i:i + size]
        bad = sum(ch in _AMBIG for ch in c)
        if bad < best_bad:
            best, best_bad = c, bad
    return best


def make_queries(sentence):
    """回傳要送出的查詢字串（最多 3 個）。"""
    clauses = [c for c in _CLAUSE.split(sentence) if len(c) >= 4]
    if not clauses:
        c = re.sub(r"\W", "", sentence)
        return [c] if len(c) >= 3 else []
    clauses.sort(key=len, reverse=True)
    qs = []
    # 1) 最長兩個子句各取一段（片段越長越精準，但容錯越低）
    for c in clauses[:2]:
        qs.append(_best_chunk(c, 7))
    # 2) 第三個查詢：最長子句的另一段（避免第一段剛好有改字）
    c0 = clauses[0]
    if len(c0) >= 12:
        tail = c0[len(c0) // 2:]
        qs.append(_best_chunk(tail, 6))
    return list(dict.fromkeys(q for q in qs if len(q) >= 3))[:3]


_BLOCK = re.compile(r'<span id="source_\d+"\s*>(.*?)</span>')
_ITEM = re.compile(
    r'w3-text-teal[^>]*>(?P<title>.*?)</span>'
    r'|<span id="page_\d+_\d+"[^>]*>(?P<page>.*?)</span>.*?'
    r'<div class="w3-xlarge w3-light-grey[^"]*"\s*>(?P<text>.*?)<a href="#4contact">', re.S)


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).replace("\xa0", " ").strip()


def parse(page):
    """回傳 [(出處, 篇章標題, 頁碼, 段落文字), ...]。"""
    out = []
    parts = _BLOCK.split(page)
    # parts = [前言, 出處1, 內容1, 出處2, 內容2, ...]
    for i in range(1, len(parts) - 1, 2):
        src = _clean(parts[i]).replace("册", "冊")
        title = ""
        for m in _ITEM.finditer(parts[i + 1]):
            if m.group("title") is not None:
                title = _clean(m.group("title")).replace("　", "　")
            else:
                out.append((src, title, _clean(m.group("page")), _clean(m.group("text"))))
    return out


def search(sentence):
    """查詢一句，回傳去重後的段落清單。"""
    paras = {}
    queries = make_queries(sentence)
    ok = 0
    with ThreadPoolExecutor(3) as ex:
        pages = list(ex.map(_fetch, queries))
    for page in pages:
        if not page:
            continue
        ok += 1
        for p in parse(page):
            paras.setdefault((p[0], p[2], p[3][:40]), p)
    if queries and not ok:
        raise ConnectionError("李常受文集線上索引連線失敗")
    return list(paras.values())
