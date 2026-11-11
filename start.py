"""啟動器：需要時先安裝套件、下載資料、建立索引，然後開啟搜尋網站。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 套件的 import 名稱（和 requirements.txt 對應）
MODULES = ["opencc", "rapidfuzz", "truststore", "pymupdf", "winocr", "PIL"]


def ensure_packages():
    missing = [m for m in MODULES if importlib.util.find_spec(m) is None]
    if not missing:
        return
    print("第一次使用：正在安裝需要的套件…", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                    str(ROOT / "requirements.txt")], check=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 以上的版本，請到 https://www.python.org/downloads/ 安裝新版。")
        sys.exit(1)
    ensure_packages()
    if not (ROOT / "data" / "index.db").exists():
        print("第一次使用：正在從各網站下載資料並建立索引。\n"
              "約需 40 分鐘到 1 小時，會占用約 2 GB 硬碟空間，請不要關閉視窗…", flush=True)
        (ROOT / "data").mkdir(exist_ok=True)
        subprocess.run([sys.executable, "-W", "ignore", str(ROOT / "crawl.py")], cwd=ROOT, check=True)
        subprocess.run([sys.executable, str(ROOT / "build_index.py")], cwd=ROOT, check=True)
    import server
    server.main()
