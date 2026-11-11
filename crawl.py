"""抓取三個網站的內容，存成 data/{ls,nee,rv}.jsonl。

每行一份文件：{"source", "title", "url", "units": [[標籤, 文字], ...], "converted": bool}
已下載的網頁會快取在 data/cache，可中斷後重跑。
用法：python crawl.py [ls] [nee] [rv]
"""
import hashlib
import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import DATA, decode, fetch, html_to_text, s2t, t2s
from books import _BOOKS

WORKERS = 4


def write(name, docs):
    path = DATA / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for d in docs:
            if d:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"{name}: 寫入 {sum(1 for d in docs if d)} 份文件 → {path}", flush=True)


def get_html(url):
    raw = fetch(url)
    return decode(raw) if raw else None


def links(html, base):
    out = []
    for h in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        out.append(urllib.parse.urljoin(base, h.strip()))
    return out


# ---------- 生命讀經 ----------
LS_INDEX = "https://lsmchinese.org/lifestudy/index.html"


def ls_page(url):
    h = get_html(url)
    if not h:
        print("  失敗", url, flush=True)
        return None
    title = re.search(r'class="msg-title">(.*?)</div>', h, re.S)
    book = re.search(r'class="ls-title">.*?<a[^>]*>(.*?)</a>', h, re.S)
    body = re.search(r'class="ls-text[^"]*">(.*?)<!--\s*end online reading', h, re.S)
    if not body:
        return None
    text = html_to_text(body.group(1))
    t = html_to_text(title.group(1)) if title else ""
    b = html_to_text(book.group(1)) if book else ""
    return {"source": "生命讀經", "title": f"{b}　{t}".strip(), "url": url,
            "units": [["", p] for p in text.split("\n")], "converted": False}


def crawl_ls():
    idx = get_html(LS_INDEX)
    book_idx = [u for u in links(idx, LS_INDEX) if u.endswith("-idx.html")]
    book_idx = list(dict.fromkeys(book_idx))
    pages = []
    for bu in book_idx:
        bh = get_html(bu)
        if not bh:
            print("  書目失敗", bu, flush=True)
            continue
        for u in links(bh, bu):
            if (re.search(r"/lifestudy/(ot/)?[A-Za-z0-9]+\d{2,3}\.html$", u)
                    and "-idx" not in u and u not in pages):
                pages.append(u)
    print(f"生命讀經：{len(book_idx)} 卷，{len(pages)} 篇", flush=True)
    with ThreadPoolExecutor(WORKERS) as ex:
        docs = list(ex.map(ls_page, pages))
    write("ls", docs)


# ---------- 倪柝聲文集 ----------
NEE_INDEX = ("http://www.christiansquare.org/倪柝聲文集/倪柝声文集的目录.htm")
NEE_NAV = re.compile(r"^(倪柝声文集|书名：.*|回目录.*|上一篇.*|下一篇.*|返回.*)$")


def nee_page(args):
    url, volume = args
    h = get_html(url)
    if not h:
        print("  失敗", url, flush=True)
        return None
    title = re.search(r"<title>(.*?)</title>", h, re.S | re.I)
    t = html_to_text(title.group(1)) if title else url.rsplit("/", 1)[-1]
    text = html_to_text(h.split("<BODY", 1)[-1] if "<BODY" in h else h)
    paras = [p for p in text.split("\n") if not NEE_NAV.match(p.replace(" ", ""))]
    paras = [p.replace("回目录", "").replace("上一篇", "").replace("下一篇", "").strip()
             for p in paras]
    paras = [p for p in paras if p]
    return {"source": "倪柝聲文集", "title": s2t(f"{volume}　{t}"), "url": url,
            "units": [["", s2t(p)] for p in paras], "converted": True}


def crawl_nee():
    idx = get_html(NEE_INDEX)
    vols = [u for u in links(idx, NEE_INDEX) if u.endswith(".htm") and "目录" in
            urllib.parse.unquote(u) and u != NEE_INDEX and "index" not in u]
    vols = list(dict.fromkeys(vols))
    jobs = []
    seen = set()
    for vu in vols:
        vh = get_html(vu)
        if not vh:
            print("  冊目錄失敗", urllib.parse.unquote(vu), flush=True)
            continue
        vt = re.search(r"<title>(.*?)</title>", vh, re.S | re.I)
        volume = html_to_text(vt.group(1)).replace("-目录", "").replace("目录", "").strip() if vt else ""
        vdir = vu.rsplit("/", 1)[0] + "/"
        for u in links(vh, vu):
            uu = urllib.parse.unquote(u)
            if (u.startswith(vdir) and uu.endswith(".htm") and "目录" not in uu
                    and "/" not in u[len(vdir):] and u not in seen):
                seen.add(u)
                jobs.append((u, volume))
    print(f"倪柝聲文集：{len(vols)} 冊，{len(jobs)} 篇", flush=True)
    with ThreadPoolExecutor(WORKERS) as ex:
        docs = list(ex.map(nee_page, jobs))
    write("nee", docs)


# ---------- 特會訓練綱要（PDF） ----------
OUTL_INDEX = "https://www.lsmchinese.org/outlines.html"


OCR_DIR = DATA / "ocr"
# Windows OCR 常見的固定錯字
OCR_FIX = {"礻也": "祂", "耤": "藉", "褁": "裏", "啓": "啟", "説": "說", "爲": "為",
           "彀": "穀", "衆": "眾", "羣": "群", "徧": "遍"}
_END = re.compile(r"[。；：！？」』）)]$")


def _ocr_page(page):
    import asyncio
    import io

    import pymupdf
    import winocr
    from PIL import Image
    pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))

    async def go():
        return await winocr.recognize_pil(img, "zh-Hant-TW")
    res = asyncio.run(go())
    return [ln.text.replace(" ", "") for ln in res.lines]


def pdf_units(raw):
    """PDF → 段落清單。有文字層就直接抽；字被轉成圖形的，用 Windows OCR 辨識。"""
    import pymupdf
    units, ocr = [], False
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            # 逐頁判斷：有些 PDF 只有頁尾版權聲明是文字，內文仍是圖形
            real = re.sub(r"©.*|.*Ministry.*|\s", "", page.get_text())
            if len(real) >= 40:
                lines = [re.sub(r"\s*\n\s*", "", b[4]).strip()
                         for b in page.get_text("blocks") if b[6] == 0]
            else:
                ocr = True
                lines = _ocr_page(page)
            buf = ""
            for ln in lines:
                if not ln or "Ministry" in ln or "©" in ln or re.fullmatch(r"[\d\s\-－第頁/]+", ln):
                    continue
                for a, b in OCR_FIX.items():
                    ln = ln.replace(a, b)
                buf += ln
                if _END.search(ln):          # 一個綱目點通常以句讀結尾
                    units.append(buf)
                    buf = ""
            if buf:
                units.append(buf)
    return units, ocr


def outl_page(args):
    url, title = args
    cache = OCR_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    raw = fetch(url)
    if not raw or not raw.startswith(b"%PDF"):
        print("  失敗", url, flush=True)
        return None
    try:
        units, ocr = pdf_units(raw)
    except Exception as e:
        print("  PDF 解析失敗", url, e, flush=True)
        return None
    if not units:
        print("  PDF 無文字", url, flush=True)
        return None
    d = {"source": "特會訓練綱要", "title": title, "url": url,
         "units": [["", u] for u in units], "converted": False, "ocr": ocr}
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


_DASH = re.compile(r"一(?=參?[一-鿿]{1,2}[一二三四五六七八九十百○]{1,3}\d)|[一\-]{2,}")


def outl_finish(d):
    """辨識後的整理（不寫入 OCR 快取，方便之後調整規則）。"""
    if d is None:
        return None
    d = dict(d)
    units = [_DASH.sub("—", u[1]) if d.get("ocr") else u[1] for u in d["units"]]
    # 有些年份的 PDF 是簡體版：轉成繁體顯示
    text = "".join(units)[:3000]
    if text and sum(a != b for a, b in zip(text, t2s(text))) / len(text) < 0.01:
        units = [s2t(u) for u in units]
        d["converted"] = True
    d["units"] = [["", u] for u in units]
    return d


def outl_list(page_url, year):
    """解析一年的綱要頁：依 <h2> 特會分組，回傳 [(pdf網址, 標題), ...]。"""
    h = get_html(page_url)
    if not h:
        return []
    jobs = []
    event, n = "", 0
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>|<a\s[^>]*href=[\"']([^\"']*lsmwebcast\.com[^\"']+\.pdf)[\"'][^>]*>(.*?)</a>",
                         h, re.S | re.I):
        if m.group(1) is not None:
            parts = [html_to_text(p) for p in re.split(r"<br\s*/?>", m.group(1))]
            parts = [p.replace("總題：", "").strip() for p in parts if p.strip()]
            event = parts[0] if parts else ""
            if len(parts) > 1:
                event += "：" + "　".join(parts[1:])
            n = 0
        else:
            n += 1
            link = html_to_text(m.group(3)).strip()
            name = f"第{n}篇" if not link.startswith("第") else ""
            title = f"{year}年 {event}　{name}{('　' if name and link else '')}{link}".strip()
            pdf = urllib.parse.urljoin(page_url, m.group(2))
            # 網站上有一個連結誤指到測試主機
            pdf = pdf.replace("http://test.www.lsmwebcast.com", "https://www.lsmwebcast.com")
            jobs.append((pdf, title))
    return jobs


def ocr_available():
    """這台電腦有沒有 Windows 的繁體中文文字辨識。"""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        return bool(OcrEngine.is_language_supported(Language("zh-Hant-TW")))
    except Exception:
        return False


def crawl_outl():
    if not ocr_available():
        print("特會訓練綱要：這台電腦沒有 Windows 的繁體中文文字辨識（OCR），先略過綱要。\n"
              "  安裝方法：設定 → 時間與語言 → 語言與地區 → 新增「中文（繁體，台灣）」，"
              "裝好後執行 python crawl.py outl 再 python build_index.py", flush=True)
        return
    idx = get_html(OUTL_INDEX)
    years = [(OUTL_INDEX, re.search(r'class="current">(\d{4})', idx).group(1))]
    for u in links(idx, OUTL_INDEX):
        m = re.search(r"archives(\d{4})\.html$", u)
        if m:
            years.append((u, m.group(1)))
    years = list(dict.fromkeys(years))
    jobs = []
    for u, y in years:
        jobs += [j for j in outl_list(u, y) if j[0] not in {x[0] for x in jobs}]
    print(f"特會訓練綱要：{len(years)} 年，{len(jobs)} 篇", flush=True)
    # 先多執行緒下載，再用多個行程做 OCR（OCR 吃 CPU）
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(lambda j: fetch(j[0]), jobs))
    from concurrent.futures import ProcessPoolExecutor
    docs = []
    with ProcessPoolExecutor(4) as ex:
        for i, d in enumerate(ex.map(outl_page, jobs, chunksize=4), 1):
            docs.append(outl_finish(d))
            if i % 50 == 0:
                print(f"  綱要 {i}/{len(jobs)}", flush=True)
    write("outl", docs)


# ---------- 肯定與否定 ----------
AC_INDEX = "https://www.lsmchinese.org/a%26c/index.htm"


def ac_page(url):
    h = get_html(url)
    if not h:
        print("  失敗", url, flush=True)
        return None
    body = re.search(r'class="affcrit-text">(.*?)(?:id="leftnav"|<!--\s*end)', h, re.S)
    if not body:
        return None
    t = re.search(r"<h1[^>]*>(.*?)</h1>", body.group(1), re.S)
    title = html_to_text(t.group(1)) if t else url.rsplit("/", 1)[-1]
    paras = html_to_text(body.group(1)).split("\n")
    return {"source": "肯定與否定", "title": title, "url": url,
            "units": [["", p] for p in paras if p != title], "converted": False}


def crawl_ac():
    idx = get_html(AC_INDEX)
    pages = [u for u in links(idx, AC_INDEX)
             if re.search(r"/a%26c/\d-\d/[^/]+\.html?$", u)]
    pages = list(dict.fromkeys(pages))
    print(f"肯定與否定：{len(pages)} 篇", flush=True)
    with ThreadPoolExecutor(WORKERS) as ex:
        docs = list(ex.map(ac_page, pages))
    write("ac", docs)


# ---------- 恢復本 ----------
RV_API = "https://www.recoveryversion.com.tw/api/"
RV_WEB = "https://www.recoveryversion.com.tw/verse/{b}/{c}"


def rv_json(q):
    raw = fetch(RV_API + q)
    try:
        return json.loads(raw.decode("utf-8")) if raw else []
    except ValueError:
        return []


def rv_book_verses(no):
    """一次抓整卷經文（每卷一個請求）。"""
    return rv_json(f"getVerses?VERSION=1&output[]=content&output[]=unit_code"
                   f"&output[]=segment_code&output[]=section_code&chapter_code={no}&ORDER=id")


def rv_chapter(args):
    no, abbr, name, ch, notes, verses = args
    url = RV_WEB.format(b=no, c=ch)
    docs = []
    vu = [[f"{abbr}{ch}:{v['segment_code']}" if v["segment_code"] else f"{abbr}{ch} 標題",
           v["content"]] for v in verses if v.get("content")]
    if vu:
        docs.append({"source": "聖經恢復本", "title": f"{name} 第{ch}章 經文", "url": url,
                     "units": vu, "converted": False})
    nu = [[f"{abbr}{ch}:{n['segment_code']} 註{n['note_num']}", n["note_content"]]
          for n in notes if n.get("note_content")]
    if nu:
        docs.append({"source": "聖經恢復本", "title": f"{name} 第{ch}章 註解", "url": url,
                     "units": nu, "converted": False})
    return docs


def rv_book(args):
    no, abbr, name = args
    docs = []
    intro = rv_json(f"getBookIntros?VERSION=1&output[]=content&output[]=note"
                    f"&chapter_code={no}&ORDER=id")
    if intro:
        docs.append({"source": "聖經恢復本", "title": f"{name} 書介", "url": RV_WEB.format(b=no, c=1),
                     "units": [["書介", i["content"]] for i in intro if i.get("content")],
                     "converted": False})
    outl = rv_json(f"getOutlines?VERSION=1&output[]=level&output[]=outline_content"
                   f"&output[]=related_chapters&output[]=related_number"
                   f"&output[]=related_section_code&chapter_code={no}&ORDER=volume_order")
    if outl:
        docs.append({"source": "聖經恢復本", "title": f"{name} 綱目", "url": RV_WEB.format(b=no, c=1),
                     "units": [["綱目", o["outline_content"]] for o in outl
                               if o.get("outline_content")], "converted": False})
    return docs


def crawl_rv():
    books = [(no, abbr, names[0]) for no, abbr, _c, _a, names in _BOOKS]
    print(f"恢復本：{len(books)} 卷", flush=True)
    # 註解：一次抓整卷
    with ThreadPoolExecutor(8) as ex:
        book_notes = list(ex.map(
            lambda b: rv_json(f"getFootnotes?VERSION=1&chapter_code={b[0]}"), books))
        book_verses = list(ex.map(lambda b: rv_book_verses(b[0]), books))
    print("  註解、經文下載完成", flush=True)
    chapters = []
    for (no, abbr, c, _a, names), notes, verses in zip(_BOOKS, book_notes, book_verses):
        if not verses:
            print(f"  經文下載失敗：{names[0]}", flush=True)
        for ch in range(1, c + 1):
            chapters.append((no, abbr, names[0], ch,
                             [n for n in notes if n.get("section_code") == ch],
                             [v for v in verses if v.get("section_code") == ch]))
    docs = []
    with ThreadPoolExecutor(8) as ex:
        for ds in ex.map(rv_chapter, chapters):
            docs.extend(ds)
        print("  經文完成", flush=True)
        for ds in ex.map(rv_book, books):
            docs.extend(ds)
    write("rv", docs)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    which = sys.argv[1:] or ["rv", "ls", "nee", "ac", "outl"]
    DATA.mkdir(parents=True, exist_ok=True)
    for w in which:
        try:
            {"ls": crawl_ls, "nee": crawl_nee, "rv": crawl_rv,
             "outl": crawl_outl, "ac": crawl_ac}[w]()
        except Exception as e:      # 一個來源失敗不影響其他來源
            print(f"【{w}】抓取失敗，先略過：{e!r}", flush=True)
