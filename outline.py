"""上傳的綱要檔 → 一個一個綱目點。支援 .docx、.pdf、.txt。"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

from common import normalize

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 綱目點開頭的編號（後面要有空白或頓號，避免把「一個」的「一」當編號）
_MARK = re.compile(
    r"^\s*(第[一二三四五六七八九十百\d]+[篇章週周]"
    r"|[壹貳參叁肆伍陸柒捌玖拾]+|[一二三四五六七八九十]+|\d{1,2}|[a-zA-Z]"
    r"|[（(][\d一二三四五六七八九十a-zA-Z]+[）)])"
    r"(?:[\s　]+|[、.．]\s*)")
_LEVELS = [(r"^[壹貳參叁肆伍陸柒捌玖拾]+$", 0), (r"^[一二三四五六七八九十]+$", 1), (r"^\d+$", 2),
           (r"^[a-zA-Z]$", 3), (r"^[（(]\d+[）)]$", 4), (r"^[（(][a-zA-Z]+[）)]$", 5)]
# 文末的參考書目（不是綱目點）
_BIBLIO = re.compile(r"^(李常受文集|倪柝聲文集|\d{4}\s*年).{0,120}(章|篇|大[壹貳參叁肆伍陸柒捌玖拾]+)[。.]?$")

# 經文出處會用到的字：書卷簡稱、數字、連接符號
_REF_CHARS = (r"\s　參見看比較，,、；;～~\-—─－上下節章至和與及\d一二三四五六七八九十百○〇廿卅壹貳叁"
              r"創出利民申書士得撒王代拉尼斯伯詩箴傳歌賽耶哀結但何珥摩俄拿彌鴻哈番該亞瑪"
              r"太可路約徒羅林加弗腓西帖提多門來雅彼猶啟前後：:。.")
_TAIL_REF = re.compile(rf"[—─－\-]{{1,2}}[{_REF_CHARS}]*\d[{_REF_CHARS}]*$")
_PAREN_REF = re.compile(rf"[（(][{_REF_CHARS}]*\d[{_REF_CHARS}]*[）)]")
_ONLY_REF = re.compile(rf"^(讀經|經文|詩歌)?[{_REF_CHARS}]*$")


# 引文結尾的出處標註：”—李常受文集一九九四至一九九七年第四册，神聖奧祕的範圍，第二章。
_CITE = re.compile(r"([”」』])\s*[—─－]{1,2}[^”」』]{2,90}$")


def _docx_paragraphs(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(W + "p"):
        parts = []
        for node in p.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag in (W + "tab", W + "br"):
                parts.append(" ")
        out.append("".join(parts))
    return out


_OCR_MARK = re.compile(r"^([壹貳參叁肆伍陸柒捌玖拾]{1,2}|[一二三四五六七八九十]{1,3}|\d{1,2}|[a-zA-Z])$")


def _ocr_page_points(page):
    """OCR 一頁，利用每個字的位置把「行首編號 + 空隙 + 內文」認出來，回傳段落清單。"""
    import asyncio
    import io
    import statistics

    import pymupdf
    import winocr
    from PIL import Image
    from crawl import OCR_FIX
    pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))

    async def go():
        return await winocr.recognize_pil(img, "zh-Hant-TW")
    res = asyncio.run(go())

    # 1) 把所有字依垂直位置排成「列」（辨識引擎常把左邊的編號當成獨立一行，順序也不一定對）
    words = []
    for ln in res.lines:
        for w in ln.words:
            r = w.bounding_rect
            words.append((r.y + r.height / 2, r.x, r.width, r.height, w.text))
    if not words:
        return []
    words.sort()
    rows, cur = [], [words[0]]
    for w in words[1:]:
        if abs(w[0] - statistics.mean(x[0] for x in cur)) < 0.55 * w[3]:
            cur.append(w)
        else:
            rows.append(cur)
            cur = [w]
    rows.append(cur)

    # 2) 每一列由左到右；行首 1～3 個字後面有明顯空隙、而且像編號，就是新的一點
    paras, prev_bottom = [], None
    for row in rows:
        row.sort(key=lambda w: w[1])
        text = "".join(w[4] for w in row)
        if "Ministry" in text or "©" in text or re.fullmatch(r"[\d\s\-－第頁/]+", text):
            continue
        h = statistics.median(w[3] for w in row)
        gaps = [row[i + 1][1] - (row[i][1] + row[i][2]) for i in range(len(row) - 1)]
        typical = statistics.median(gaps) if gaps else 0
        marker = ""
        for k in (1, 2, 3):
            if len(row) > k and gaps[k - 1] > max(0.6 * h, 2.5 * typical + 2):
                head = "".join(w[4] for w in row[:k])
                if _OCR_MARK.match(head):
                    marker, text = head, "".join(w[4] for w in row[k:])
                break
        for a, b in OCR_FIX.items():
            text = text.replace(a, b)
        top = min(w[0] - w[3] / 2 for w in row)
        new_block = prev_bottom is not None and top - prev_bottom > 1.2 * h   # 行距明顯變大
        if marker or not paras or new_block:
            paras.append((marker + " " if marker else "") + text)
        else:
            paras[-1] += text
        prev_bottom = max(w[0] + w[3] / 2 for w in row)
    return paras


# 附經文的出處行，如「約14:10」「林前15:45 下…」「約貳1」
_VERSE_REF = re.compile(r"^[一-鿿]{1,3}\s?\d+(:\d+|[~\-]\d+|$)")
_CJK_MARK = re.compile(r"^[壹貳參叁肆伍陸柒捌玖拾一二三四五六七八九十]+$")
_ENDS = re.compile(r"[^—─－、，,~～（(]$")      # 上一行不是停在破折號、頓號、逗號等「還沒講完」的地方


IMG_MARK = "\x00"       # 用小圖片貼上去的數字編號（文字層裏沒有那個數字）
# 某些排版字型把字放在私用區，讀出來是亂碼：轉回正常的字
_PUA = {0xE5E3: "着", 0xE5F0: "參"}


def _fix_text(s):
    return s.translate(_PUA)


def _text_page_rows(page):
    """有文字層的一頁 → 一列一列 [(字級, [片段文字…])]，已去掉上標註腳號和頁碼。"""
    import unicodedata
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if not (s["flags"] & 1) and s["text"].strip()]
            if not spans:
                continue
            text = _fix_text("".join(s["text"] for s in spans).strip())
            # 全形的編號（ａｂｃ、１２３）轉成半形
            if len(text) <= 3:
                text = unicodedata.normalize("NFKC", text)
            size = max(s["size"] for s in spans)
            x, y = spans[0]["bbox"][0], (ln["bbox"][1] + ln["bbox"][3]) / 2
            lines.append((y, x, size, text))
    # 行首的小圖片（約一個字大小）當成數字編號
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        if x1 - x0 <= 14 and y1 - y0 <= 16:
            lines.append(((y0 + y1) / 2, x0, 0, IMG_MARK))
    lines.sort()
    rows = []
    for y, x, size, text in lines:
        if rows and abs(y - rows[-1]["y"]) < 0.45 * max(size, rows[-1]["size"]):
            rows[-1]["segs"].append((x, text))
            rows[-1]["size"] = max(rows[-1]["size"], size)
        else:
            rows.append({"y": y, "size": size, "segs": [(x, text)]})
    out = []
    for r in rows:
        segs = [t for _, t in sorted(r["segs"])]
        whole = "".join(segs)
        if "Ministry" in whole or "©" in whole or re.fullmatch(r"[\d\s\-－第頁/]+", whole):
            continue
        out.append((round(r["size"], 1), segs))
    return out


def _rows_to_paragraphs(rows):
    """依編號、字級把列組成綱目點；附的經文和文末註腳不算。"""
    # 綱目點用的最小字級（以中文編號的列為準），比它小的編號列當作註腳
    cjk_sizes = [size for size, segs in rows if len(segs) > 1 and _CJK_MARK.match(segs[0].strip())]
    min_outline = min(cjk_sizes) if cjk_sizes else 0
    paras, cur_size, verse_size, img_no = [], None, None, 0
    for size, segs in rows:
        if segs[0] == IMG_MARK:
            if len(segs) == 1:
                continue                               # 孤立的小圖，不是編號
            img_no += 1                                # 圖片編號：依出現順序算 1、2、3…
            segs = [str(img_no)] + segs[1:]
        segs = [s for s in segs if s != IMG_MARK]
        first = segs[0].strip()
        is_marker = len(segs) > 1 and bool(_OCR_MARK.match(first))
        if is_marker and _CJK_MARK.match(first):
            img_no = 0                                 # 換到新的大點／中點，小點重新從 1 算
        if is_marker and size < min_outline - 0.4:
            continue                                   # 文末註腳（1 李常受文集…）
        # 上一點還沒寫完（不是以句讀結尾），這一行卻以數字開頭：是換行，不是新的一點
        if (is_marker and not _CJK_MARK.match(first) and paras and cur_size == size
                and verse_size is None and not _ENDS.search(paras[-1])):
            paras[-1] += "".join(segs)
            continue
        small = bool(min_outline) and size < min_outline - 0.4
        if _VERSE_REF.match(first) and not is_marker and (not min_outline or size < min_outline - 0.4
                                                          or cur_size is None or size != cur_size):
            verse_size = size                          # 進入經文區塊
            continue
        if verse_size is not None and not is_marker and size == verse_size:
            continue                                   # 經文內文
        if small and not is_marker and size != cur_size:
            continue                                   # 比綱目點小的字（經文、註腳）
        verse_size = None
        if is_marker:
            paras.append(first + " " + "".join(segs[1:]))
            cur_size = size
        elif paras and cur_size == size and (
                not (m := _MARK.match("".join(segs)))
                or (not _CJK_MARK.match(m.group(1)) and not _ENDS.search(paras[-1]))):
            paras[-1] += "".join(segs)                 # 同一點的續行（含換行後剛好以數字開頭的情況）
        else:
            paras.append("".join(segs))                # 沒有編號的行（標題等），或編號和內文沒分開
            cur_size = size
    return paras


def _pdf_paragraphs(raw):
    import pymupdf
    from crawl import _DASH
    rows, ocr_paras, ocr = [], [], False
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            real = re.sub(r"©.*|.*Ministry.*|\s", "", page.get_text())
            if len(real) >= 40:      # 有文字層
                rows += _text_page_rows(page)
            else:                    # 沒有文字層：文字辨識
                ocr = True
                ocr_paras += [_DASH.sub("—", p) for p in _ocr_page_points(page)]
    return _rows_to_paragraphs(rows) + ocr_paras, ocr


def read_paragraphs(filename, raw):
    """回傳 (段落清單, 是否經過 OCR)。"""
    name = filename.lower()
    if name.endswith(".docx"):
        return _docx_paragraphs(raw), False
    if name.endswith(".pdf"):
        return _pdf_paragraphs(raw)
    if name.endswith(".doc"):
        raise ValueError("不支援舊版 .doc 檔，請在 Word 裏另存成 .docx 再上傳。")
    for enc in ("utf-8-sig", "utf-16", "big5", "gb18030"):
        try:
            return raw.decode(enc).splitlines(), False
        except UnicodeError:
            continue
    raise ValueError("讀不出這個檔案的文字，請改用 .docx、.pdf 或 .txt。")


def clean_query(text):
    """去掉句尾、括號裏的經文出處，只留綱目點本身的話。"""
    q = _CITE.sub(lambda m: m.group(1), text)      # 引文後面的「—李常受文集…第二章。」
    q = _PAREN_REF.sub("", q)
    for _ in range(2):
        q = _TAIL_REF.sub("", q).rstrip(" ：:")
    return q.strip()


def to_points(paragraphs):
    points = []
    for p in paragraphs:
        text = re.sub(r"[ \t　]+", " ", p).strip()
        if not text:
            continue
        m = _MARK.match(text)
        marker, body = (m.group(1), text[m.end():].strip()) if m else ("", text)
        level = next((lv for pat, lv in _LEVELS if re.match(pat, marker)), 0)
        query = clean_query(body)
        skip = ""
        if _ONLY_REF.match(body) or len(normalize(query)) < 6:
            skip = "太短或只有經文出處，略過"
        elif _BIBLIO.match(body):
            skip = "參考書目，略過"
        points.append({"marker": marker, "text": body, "level": level,
                       "query": query, "skip": skip})
    return points


def parse(filename, raw):
    paragraphs, ocr = read_paragraphs(filename, raw)
    return {"points": to_points(paragraphs), "ocr": ocr}
