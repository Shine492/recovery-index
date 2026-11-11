"""共用工具：下載（含快取）、HTML 取文字、繁簡正規化、分句。"""
import hashlib
import html
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
UA = "Mozilla/5.0 (personal source-search tool)"


# ---------- 下載 ----------
def norm_url(url):
    """把含中文、空白的網址轉成合法網址（路徑用 UTF-8 百分比編碼）。"""
    p = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def fetch(url, retries=4, delay=0.15):
    """下載網址並快取到 data/cache；回傳原始位元組（失敗回傳 None）。"""
    url = norm_url(url)
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if f.exists():
        return f.read_bytes()
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=40).read()
            f.write_bytes(raw)
            time.sleep(delay)
            return raw
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def decode(raw):
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I)
    enc = (m.group(1).decode().lower() if m else "utf-8")
    if enc in ("gbk", "gb2312", "gb18030"):
        enc = "gb18030"
    return raw.decode(enc, errors="replace")


# ---------- HTML → 文字 ----------
def html_to_text(s):
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)
    s = re.sub(r"(?s)<!--.*?-->", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h\d|table)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [ln.strip(" \t\r　") for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# ---------- 繁簡 ----------
def _load_map(name):
    import opencc
    d = Path(opencc.__file__).parent / "dictionary" / name
    m = {}
    for line in d.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        k, v = line.split("\t", 1)
        if len(k) == 1:
            m[ord(k)] = v.split(" ")[0]
    return m


_T2S = None
_S2T = None


def t2s(s):
    """字對字轉簡體（只用來比對）。"""
    global _T2S
    if _T2S is None:
        _T2S = _load_map("TSCharacters.txt")
        # 恢復本常用異體字，統一起來
        for a, b in {"裏": "里", "纔": "才", "祂": "他", "牠": "它", "麼": "么",
                     "喫": "吃", "豫": "预", "眞": "真", "爲": "为", "著": "着", "於": "于",
                     "藉": "借", "那": "哪"}.items():
            _T2S[ord(a)] = b
    return s.translate(_T2S)


_CC = None


def s2t(s):
    """簡體轉繁體（顯示用，詞彙層級，臺灣用字）。"""
    global _CC
    if _CC is None:
        from opencc import OpenCC
        _CC = OpenCC("s2tw")
    return _CC.convert(s)


_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(s):
    """比對用：轉簡體、去掉標點空白、英數轉小寫。"""
    return _PUNCT.sub("", t2s(s)).lower()


# ---------- 分句 ----------
_SPLIT = re.compile(r"(?<=[。！？；!?;])(?![」』”’）\)])|\n")


def split_sentences(text, min_len=4):
    out = []
    for part in _SPLIT.split(text):
        part = part.strip()
        if len(normalize(part)) >= min_len:
            out.append(part)
    return out
