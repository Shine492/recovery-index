"""關鍵詞搜尋：最多 4 個詞（空白分隔），找出同一段裏全部出現的段落，
並截出約 30 字的原文片段（詞以 <mark> 標示）。"""
import html
import re

import cwwl
from common import normalize, s2t, t2s

MAX_TERMS = 4
MAX_SCAN = 4000        # 每個來源最多檢查幾段
MAX_SHOW = 60          # 每個來源最多列出幾筆
SNIP = 30              # 片段目標長度（字）
FAR = 90               # 詞彼此距離超過這個字數，就分段顯示


def parse_terms(text):
    terms = [t for t in re.split(r"\s+", text.strip()) if normalize(t)]
    terms = list(dict.fromkeys(terms))
    return terms[:MAX_TERMS], len(terms) > MAX_TERMS


def looks_like_keywords(text):
    """自動判斷：沒有句讀標點、每個詞不長、最多幾個詞 → 當關鍵詞搜尋。"""
    t = text.strip()
    if not t or re.search(r"[，。！？；：、,.!?;:\n「」『』（）()]", t):
        return False
    parts = t.split()
    return all(len(p) <= 10 for p in parts) and len(parts) <= 8


# ---------- 片段 ----------
def _norm_map(text):
    """回傳 (正規化字串, 每個正規化字元在原文中的位置)。"""
    tt = t2s(text).lower()
    if len(tt) == len(text):            # 一般情況：字對字，走快速路徑
        pos = [i for i, c in enumerate(tt) if c.isalnum()]
        return "".join(tt[i] for i in pos), pos
    norm, pos = [], []
    for i, ch in enumerate(text):
        for c in normalize(ch):
            norm.append(c)
            pos.append(i)
    return "".join(norm), pos


def _occurrences(norm, tn):
    out, i = [], norm.find(tn)
    while i >= 0:
        out.append(i)
        i = norm.find(tn, i + 1)
    return out


def _min_window(occ, tlens):
    """occ[k] = 第 k 個詞在正規化字串中的位置清單；回傳涵蓋全部詞的最小範圍 (起, 迄)。"""
    events = sorted((p, k) for k, ps in enumerate(occ) for p in ps)
    need, have, cnt = len(occ), {}, 0
    best, j = None, 0
    for i, (p, k) in enumerate(events):
        have[k] = have.get(k, 0) + 1
        if have[k] == 1:
            cnt += 1
        while cnt == need:
            s, ks = events[j]
            end = max(pp + tlens[kk] for pp, kk in events[j:i + 1])
            if best is None or end - s < best[1] - best[0]:
                best = (s, end)
            have[ks] -= 1
            if have[ks] == 0:
                cnt -= 1
            j += 1
    return best


def _mark(text, tnorms):
    """把 text 中所有詞（以正規化比對）標上 <mark>。"""
    norm, pos = _norm_map(text)
    spans = []
    for tn in tnorms:
        for p in _occurrences(norm, tn):
            spans.append((pos[p], pos[p + len(tn) - 1] + 1))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, last = [], 0
    for a, b in merged:
        out.append(html.escape(text[last:a]) + "<mark>" + html.escape(text[a:b]) + "</mark>")
        last = b
    out.append(html.escape(text[last:]))
    return "".join(out)


def _expand(text, a, b, size=SNIP):
    """把原文範圍 [a,b) 往兩邊擴到約 size 字。"""
    extra = max(0, size - (b - a))
    a2 = max(0, a - extra // 2)
    b2 = min(len(text), b + (extra - (a - a2)))
    a2 = max(0, min(a2, b2 - size))
    return a2, b2


def snippet(text, tnorms):
    """回傳 (片段 HTML, 詞的最小距離)；若不是每個詞都出現，回傳 None。"""
    norm, pos = _norm_map(text)
    occ = [_occurrences(norm, tn) for tn in tnorms]
    if not all(occ):
        return None
    win = _min_window(occ, [len(t) for t in tnorms])
    a, b = pos[win[0]], pos[win[1] - 1] + 1
    if b - a <= FAR:
        ranges = [_expand(text, a, b)]
    else:
        # 詞彼此太遠：每個詞各取一小段
        ranges = []
        for k, tn in enumerate(tnorms):
            p = min((p for p in occ[k] if win[0] <= p < win[1]), default=occ[k][0])
            ranges.append(_expand(text, pos[p], pos[p + len(tn) - 1] + 1, 24))
        ranges.sort()
        merged = []
        for r in ranges:
            if merged and r[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
            else:
                merged.append(r)
        ranges = merged
    parts = []
    for i, (x, y) in enumerate(ranges):
        pre = "…" if x > 0 else ""
        post = "…" if y < len(text) and i == len(ranges) - 1 else ""
        parts.append(pre + _mark(text[x:y], tnorms) + post)
    return "".join(parts), win[1] - win[0]


SHORT = 20   # 段落短於這個字數（多半是標題），就接下一段補足片段


def _pad_short(snip_html, text, following):
    """段落太短時，接上後面段落的開頭，讓片段至少約 SNIP 字。"""
    need = SNIP - len(text)
    tail = "".join(following)[:need]
    if not tail:
        return snip_html
    more = "…" if len("".join(following)) > need else ""
    return snip_html + '<span class="ctx">　' + html.escape(tail) + more + "</span>"


# ---------- 本機 ----------
def _local(db, tnorms):
    q = " AND ".join('"' + " ".join(tn) + '"' for tn in tnorms)
    ids = [r[0] for r in db.execute(
        "SELECT rowid FROM paras_fts WHERE paras_fts MATCH ? LIMIT ?", (q, MAX_SCAN + 1))]
    capped = len(ids) > MAX_SCAN
    ids = ids[:MAX_SCAN]
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = db.execute(
            f"SELECT p.id, p.label, p.disp, d.source, d.title, d.url, d.converted "
            f"FROM paras p JOIN docs d ON d.id = p.doc_id WHERE p.id IN "
            f"({','.join('?' * len(chunk))}) ORDER BY p.id", chunk).fetchall()
        for r in rows:
            sn = snippet(r["disp"], tnorms)
            if not sn:
                continue
            html_ = sn[0]
            short = len(r["disp"]) < SHORT
            if short:
                nxt = db.execute(
                    "SELECT disp FROM paras WHERE id > ? AND id <= ? AND doc_id = "
                    "(SELECT doc_id FROM paras WHERE id = ?) ORDER BY id",
                    (r["id"], r["id"] + 3, r["id"])).fetchall()
                html_ = _pad_short(html_, r["disp"], [x[0] for x in nxt])
            out.append({"source": r["source"], "title": r["title"], "label": r["label"],
                        "url": r["url"], "converted": bool(r["converted"]),
                        "snippet": html_, "dist": sn[1], "short": short, "order": r["id"]})
    return out, capped


# ---------- 李常受文集 ----------
def _cwwl(terms, tnorms):
    q = " ".join(s2t(t) if t2s(t) == t else t for t in terms)
    page = cwwl._fetch(q, timeout=40)
    if page is None:
        return None
    out = []
    paras = cwwl.parse(page)
    for i, (src, title, page_no, text) in enumerate(paras):
        sn = snippet(text, tnorms)
        if not sn:
            continue
        html_ = sn[0]
        short = len(text) < SHORT
        if short:
            nxt = [p[3] for p in paras[i + 1:i + 4] if p[0] == src]
            html_ = _pad_short(html_, text, nxt)
        out.append({"source": "李常受文集", "title": title, "label": f"{src}　{page_no}",
                    "url": cwwl.SITE, "converted": False,
                    "snippet": html_, "dist": sn[1], "short": short, "order": i})
    return out


def search(db, text, use_cwwl=True, db_ok=True):
    terms, truncated = parse_terms(text)
    tnorms = list(dict.fromkeys(normalize(t) for t in terms))
    hits, capped = _local(db, tnorms) if (db_ok and tnorms) else ([], False)
    cwwl_failed = False
    if use_cwwl and tnorms:
        c = _cwwl(terms, tnorms)
        if c is None:
            cwwl_failed = True
        else:
            hits += c
    groups = {}
    for h in hits:
        groups.setdefault(h["source"], []).append(h)
    result = {}
    for src, items in groups.items():
        # 詞越靠近越前面；同距離依原書順序
        items.sort(key=lambda h: (h["dist"] > FAR, h["short"], h["order"]))
        result[src] = {"total": len(items), "items": items[:MAX_SHOW]}
    disp_terms = [s2t(t) if (t2s(t) == t and s2t(t) != t) else t for t in terms]
    return {
        "mode": "keyword",
        "terms": disp_terms,
        "groups": result,
        "truncated": truncated,
        "capped": capped,
        "input_converted": disp_terms != terms,
        "nee_converted": "倪柝聲文集" in result,
        "cwwl_failed": cwwl_failed,
    }
