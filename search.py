"""比對核心：輸入一段文字，逐句找出出處或相似度 ≥ 門檻的句子。

本機索引：聖經恢復本、生命讀經、倪柝聲文集（data/index.db）
即時查詢：李常受文集線上索引（cwwl.twgbr.org）
"""
import html
import re
import sqlite3
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz

import cwwl
from common import DATA, normalize, s2t, split_sentences, t2s

MAX_TERMS = 14       # 每句最多用幾個稀有三字元去找候選
MAX_CANDIDATES = 400
MAX_RESULTS = 30     # 每句最多列出幾筆
SOURCE_ORDER = ["聖經恢復本", "生命讀經", "倪柝聲文集", "李常受文集", "特會訓練綱要", "肯定與否定"]
KIND_ORDER = ["完全相同", "原文包含此句", "整句相似", "部分相似"]

_local = threading.local()


def db():
    if not hasattr(_local, "con"):
        con = sqlite3.connect(f"file:{DATA / 'index.db'}?mode=ro", uri=True,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        _local.con = con
    return _local.con


# ---------- 直達原文的連結 ----------
_CLAUSE = re.compile(r"[，,。．！!？?；;：:、「」『』“”‘’（）()《》〈〉—─…\s]+")
_cc_t2s = None


def _fragment(text, converted=False):
    """網址的「文字定位」片段：瀏覽器開啟後會捲到這段字並標示出來（Chrome、Edge、Safari 支援）。"""
    global _cc_t2s
    head = re.split(r"\s", text.strip(), maxsplit=1)[0][:14]
    if len(normalize(head)) < 4:
        return ""
    if converted:            # 原網頁是簡體：轉回簡體才對得上（盡力而為，對不上時只是不會自動捲動）
        if _cc_t2s is None:
            from opencc import OpenCC
            _cc_t2s = OpenCC("tw2s")
        head = _cc_t2s.convert(head)
    return "#:~:text=" + urllib.parse.quote(head, safe="").replace("-", "%2D")


def deep_link(source, url, text, converted=False):
    """把文件網址變成盡量直達那一句的網址。"""
    if source in ("生命讀經", "肯定與否定", "倪柝聲文集"):
        return url + _fragment(text, converted)
    return url                # 恢復本：到該章；特會綱要：開啟該篇 PDF


def cwwl_link(text):
    """李常受文集線上索引沒有固定網址，只能用它的搜尋表單：
    連到本機的中轉頁，由中轉頁自動在該網站搜尋這句話裏最長的一段。"""
    clauses = [c for c in _CLAUSE.split(text) if c]
    if not clauses:
        return cwwl.SITE
    q = max(clauses, key=len)[:16]
    return "/go/cwwl?q=" + urllib.parse.quote(q, safe="")


# ---------- 評分 ----------
def score_window(n, prev_n, cur_n, next_n):
    """n 與「本句」的相似度。回傳 (分數, 類型, 對齊範圍)；
    對齊範圍是在 prev+cur+next 串起來的正規化字串中的 (起, 迄)。"""
    off = len(prev_n)
    ratio = fuzz.ratio(n, cur_n)
    if ratio >= 99.99:
        return 100.0, "完全相同", (off, off + len(cur_n))
    best = (ratio, "整句相似", (off, off + len(cur_n)))
    window = prev_n + cur_n + next_n
    if 6 <= len(n) <= len(window):
        al = fuzz.partial_ratio_alignment(n, window)
        # 對齊範圍必須落在本句裏（至少一半），否則算鄰句的
        if al and al.score > best[0] + 0.01:
            inside = min(al.dest_end, off + len(cur_n)) - max(al.dest_start, off)
            if inside >= (al.dest_end - al.dest_start) / 2:
                kind = "原文包含此句" if al.score >= 99.99 else "部分相似"
                best = (al.score, kind, (al.dest_start, al.dest_end))
    return best


def _norm_pos(text):
    pos = []
    for i, ch in enumerate(text):
        pos.extend([i] * len(normalize(ch)))
    return pos


def highlight(text, start, end):
    pos = _norm_pos(text)
    if not pos or start >= end:
        return html.escape(text)
    a = pos[min(start, len(pos) - 1)]
    b = pos[min(end, len(pos)) - 1] + 1
    return (html.escape(text[:a]) + "<mark>" + html.escape(text[a:b]) + "</mark>"
            + html.escape(text[b:]))


# ---------- 本機索引 ----------
def _rare_trigrams(n):
    tris = list(dict.fromkeys(n[i:i + 3] for i in range(len(n) - 2)))
    counted = []
    for t in tris:
        r = db().execute("SELECT doc FROM vocab WHERE term=?", (t,)).fetchone()
        if r:
            counted.append((r[0], t))
    counted.sort()
    return [t for _, t in counted[:MAX_TERMS]]


def _candidates(n):
    if len(n) < 3:
        return []
    terms = _rare_trigrams(n)
    if not terms:
        return []
    q = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = db().execute(
        "SELECT rowid FROM sents_fts WHERE sents_fts MATCH ? ORDER BY bm25(sents_fts) LIMIT ?",
        (q, MAX_CANDIDATES)).fetchall()
    return [r[0] for r in rows]


def _sent(sid):
    return db().execute("SELECT * FROM sents WHERE id=?", (sid,)).fetchone()


def _neighbor(row, delta):
    if row is None:
        return None
    r = _sent(row["id"] + delta)
    return r if r is not None and r["doc_id"] == row["doc_id"] else None


def _local_search(n, threshold):
    if not (DATA / "index.db").exists():
        return []
    results, seen = [], set()
    for sid in _candidates(n):
        row = _sent(sid)
        prev, nxt = _neighbor(row, -1), _neighbor(row, 1)
        pn = prev["norm"] if prev is not None else ""
        nn = nxt["norm"] if nxt is not None else ""
        score, kind, span = score_window(n, pn, row["norm"], nn)
        if score < threshold or sid in seen:
            continue
        seen.add(sid)
        doc = db().execute("SELECT * FROM docs WHERE id=?", (row["doc_id"],)).fetchone()
        pd = prev["disp"] if prev is not None else ""
        nd = nxt["disp"] if nxt is not None else ""
        # 只把命中範圍真正碰到的鄰句放進「片段」，其餘當前後文
        snippet = highlight(pd + row["disp"] + nd, *span)
        before = _neighbor(prev, -1) if prev is not None else None
        after = _neighbor(nxt, 1) if nxt is not None else None
        results.append({
            "score": round(score, 1), "kind": kind, "source": doc["source"],
            "title": doc["title"], "label": row["label"],
            "url": deep_link(doc["source"], doc["url"], row["disp"], bool(doc["converted"])),
            "converted": bool(doc["converted"]), "snippet": snippet,
            "before": html.escape(before["disp"]) if before is not None else "",
            "after": html.escape(after["disp"]) if after is not None else "",
        })
    return results


# ---------- 李常受文集（即時） ----------
def _cwwl_search(sentence, n, threshold):
    out = []
    try:
        paras = cwwl.search(sentence)
    except Exception:
        return [], False
    for src, title, page, text in paras:
        sents = split_sentences(text, min_len=1) or [text]
        norms = [normalize(s) for s in sents]
        best = None
        for i, cur in enumerate(norms):
            if not cur:
                continue
            pn = norms[i - 1] if i > 0 else ""
            nn = norms[i + 1] if i + 1 < len(norms) else ""
            sc = score_window(n, pn, cur, nn)
            if best is None or sc[0] > best[0][0]:
                best = (sc, i)
        if best is None or best[0][0] < threshold:
            continue
        (score, kind, span), i = best
        pd = sents[i - 1] if i > 0 else ""
        nd = sents[i + 1] if i + 1 < len(sents) else ""
        out.append({
            "score": round(score, 1), "kind": kind, "source": "李常受文集",
            "title": title, "label": f"{src}　{page}", "url": cwwl_link(sents[i]),
            "converted": False, "snippet": highlight(pd + sents[i] + nd, *span),
            "before": html.escape("".join(sents[max(0, i - 2):max(0, i - 1)])),
            "after": html.escape("".join(sents[i + 2:i + 3])),
        })
    return out, True


# ---------- 對外 ----------
def search(text, threshold=90.0, use_cwwl=True):
    text = text.strip()
    sents = split_sentences(text, min_len=3) or ([text] if text else [])
    sents = sents[:40]
    norms = [normalize(s) for s in sents]

    cw = [([], True)] * len(sents)
    if use_cwwl:
        with ThreadPoolExecutor(4) as ex:
            futs = [ex.submit(_cwwl_search, s, n, threshold) for s, n in zip(sents, norms)]
            local = [_local_search(n, threshold) for n in norms]
            cw = [f.result() for f in futs]
    else:
        local = [_local_search(n, threshold) for n in norms]

    out = []
    input_converted = False
    cwwl_failed = False
    for s, loc, (cr, ok) in zip(sents, local, cw):
        cwwl_failed |= not ok
        disp = s
        if t2s(s) == s and s2t(s) != s:      # 使用者輸入的是簡體
            disp = s2t(s)
            input_converted = True
        res = loc + cr
        res.sort(key=lambda r: (-r["score"], KIND_ORDER.index(r["kind"]),
                                SOURCE_ORDER.index(r["source"])))
        out.append({"query": disp, "results": res[:MAX_RESULTS]})
    return {
        "sentences": out,
        "input_converted": input_converted,
        "nee_converted": any(r["converted"] for q in out for r in q["results"]),
        "cwwl_failed": cwwl_failed,
        "sources_missing": [] if (DATA / "index.db").exists() else ["本機索引尚未建立"],
    }
