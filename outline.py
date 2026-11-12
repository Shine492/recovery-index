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
    r"|[壹貳參肆伍陸柒捌玖拾]+|[一二三四五六七八九十]+|\d{1,2}|[a-zA-Z]"
    r"|[（(][\d一二三四五六七八九十a-zA-Z]+[）)])"
    r"(?:[\s　]+|[、.．]\s*)")
_LEVELS = [(r"^[壹貳參肆伍陸柒捌玖拾]+$", 0), (r"^[一二三四五六七八九十]+$", 1), (r"^\d+$", 2),
           (r"^[a-zA-Z]$", 3), (r"^[（(]\d+[）)]$", 4), (r"^[（(][a-zA-Z]+[）)]$", 5)]

# 經文出處會用到的字：書卷簡稱、數字、連接符號
_REF_CHARS = (r"\s　參見看比較，,、；;～~\-—─－上下節章至和與及\d一二三四五六七八九十百○〇廿卅"
              r"創出利民申書士得撒王代拉尼斯伯詩箴傳歌賽耶哀結但何珥摩俄拿彌鴻哈番該亞瑪"
              r"太可路約徒羅林加弗腓西帖提多門來雅彼猶啟前後：:。.")
_TAIL_REF = re.compile(rf"[—─－\-]{{1,2}}[{_REF_CHARS}]*\d[{_REF_CHARS}]*$")
_PAREN_REF = re.compile(rf"[（(][{_REF_CHARS}]*\d[{_REF_CHARS}]*[）)]")
_ONLY_REF = re.compile(rf"^(讀經|經文|詩歌)?[{_REF_CHARS}]*$")


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


_OCR_MARK = re.compile(r"^([壹貳參肆伍陸柒捌玖拾]{1,2}|[一二三四五六七八九十]{1,3}|\d{1,2}|[a-zA-Z])$")


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


def _pdf_paragraphs(raw):
    import pymupdf
    from crawl import _DASH
    paras, ocr = [], False
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            real = re.sub(r"©.*|.*Ministry.*|\s", "", page.get_text())
            if len(real) >= 40:      # 有文字層：照文字行讀，行首有編號就是新的一點
                for line in page.get_text().splitlines():
                    line = line.strip()
                    if not line or "Ministry" in line or re.fullmatch(r"[\d\s\-－第頁/]+", line):
                        continue
                    if _MARK.match(line) or not paras:
                        paras.append(line)
                    else:
                        paras[-1] += line
            else:                    # 沒有文字層：文字辨識
                ocr = True
                paras += [_DASH.sub("—", p) for p in _ocr_page_points(page)]
    return paras, ocr


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
    q = _PAREN_REF.sub("", text)
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
        points.append({"marker": marker, "text": body, "level": level,
                       "query": query, "skip": skip})
    return points


def parse(filename, raw):
    paragraphs, ocr = read_paragraphs(filename, raw)
    return {"points": to_points(paragraphs), "ocr": ocr}
