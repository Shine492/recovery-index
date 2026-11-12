"""本機搜尋網站。用法：python server.py ，然後開 http://localhost:8765"""
import json
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kwsearch
import search

PORT = 8765
PAGE_FILE = Path(__file__).parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/outline":
            return self.do_outline()
        if self.path != "/api/search":
            return self._send(404, b"not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            text = str(req.get("text", ""))[:5000]
            th = float(req.get("threshold", 90))
            use_cwwl = bool(req.get("cwwl", True))
            mode = req.get("mode", "auto")
            if mode == "keyword" or (mode == "auto" and kwsearch.looks_like_keywords(text)):
                res = kwsearch.search(search.db(), text, use_cwwl,
                                      db_ok=(search.DATA / "index.db").exists())
            else:
                res = search.search(text, max(50.0, min(100.0, th)), use_cwwl)
                res["mode"] = "sentence"
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as e:  # 回報錯誤給頁面
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")

    def do_outline(self):
        """上傳綱要檔（原始位元組，檔名放在 X-Filename 標頭），回傳拆好的綱目點。"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 30 * 1024 * 1024:
                raise ValueError("檔案太大（上限 30 MB）。")
            name = urllib.parse.unquote(self.headers.get("X-Filename", "outline.txt"))
            import outline
            res = outline.parse(name, self.rfile.read(n))
            if not res["points"]:
                raise ValueError("這個檔案裏讀不到文字。")
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as e:
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(400, body, "application/json; charset=utf-8")

    def log_message(self, *a):
        pass


def lan_ips():
    """這台電腦在區域網路上的 IPv4 位址。"""
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        # 0.0.0.0：同一個 Wi-Fi／區域網路內的手機、平板也能連
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        # 已經有一個在跑了，直接開瀏覽器
        print(f"搜尋網站已經在執行中：http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
        return
    url = f"http://localhost:{PORT}"
    print(f"搜尋網站已啟動：{url}（關閉此視窗即停止）")
    for ip in lan_ips():
        print(f"手機（連同一個 Wi-Fi）請開：http://{ip}:{PORT}")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
