"""把 data/*.jsonl 切成句子，建立 data/index.db（SQLite + FTS5 三字元索引）。
用法：python build_index.py
"""
import json
import sqlite3
import sys

from common import DATA, normalize, split_sentences

SOURCES = ["rv", "ls", "nee", "outl", "ac"]


def main():
    db_path = DATA / "index.db"
    tmp = DATA / "index.db.tmp"
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE docs(id INTEGER PRIMARY KEY, source TEXT, title TEXT, url TEXT,
                          converted INTEGER);
        CREATE TABLE sents(id INTEGER PRIMARY KEY, doc_id INTEGER, label TEXT,
                           disp TEXT, norm TEXT);
        CREATE VIRTUAL TABLE sents_fts USING fts5(norm, content='sents',
                           content_rowid='id', tokenize='trigram');
        -- 段落（關鍵詞搜尋用）：每個字當一個詞元，片語查詢 = 子字串查詢，2 字詞也能用索引
        CREATE TABLE paras(id INTEGER PRIMARY KEY, doc_id INTEGER, label TEXT, disp TEXT);
        CREATE VIRTUAL TABLE paras_fts USING fts5(t, content='', tokenize='unicode61');
    """)
    doc_id = sent_id = para_id = 0
    for name in SOURCES:
        path = DATA / f"{name}.jsonl"
        if not path.exists():
            print(f"略過 {name}（尚未抓取）")
            continue
        n_docs = n_sents = 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                doc_id += 1
                n_docs += 1
                con.execute("INSERT INTO docs VALUES(?,?,?,?,?)",
                            (doc_id, d["source"], d["title"], d["url"], int(d["converted"])))
                rows, prows, frows = [], [], []
                for label, text in d["units"]:
                    for s in split_sentences(text):
                        sent_id += 1
                        rows.append((sent_id, doc_id, label, s, normalize(s)))
                    n = normalize(text)
                    if n:
                        para_id += 1
                        prows.append((para_id, doc_id, label, text))
                        frows.append((para_id, " ".join(n)))
                con.executemany("INSERT INTO sents VALUES(?,?,?,?,?)", rows)
                con.executemany("INSERT INTO paras VALUES(?,?,?,?)", prows)
                con.executemany("INSERT INTO paras_fts(rowid, t) VALUES(?,?)", frows)
                n_sents += len(rows)
        print(f"{name}: {n_docs} 份文件，{n_sents} 句", flush=True)
    print("建立全文索引中…", flush=True)
    con.execute("INSERT INTO sents_fts(sents_fts) VALUES('rebuild')")
    con.execute("CREATE INDEX sents_doc ON sents(doc_id)")
    con.execute("INSERT INTO paras_fts(paras_fts) VALUES('optimize')")
    con.execute("CREATE VIRTUAL TABLE vocab USING fts5vocab('sents_fts', 'row')")
    con.commit()
    con.execute("VACUUM")
    con.close()
    if db_path.exists():
        db_path.unlink()
    tmp.rename(db_path)
    print(f"完成：{db_path}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
