import os
import glob
import sqlite3
import shutil
import tempfile
import json

def extract():
    # 1. Cookies
    pattern_c = os.path.expanduser("~/snap/firefox/common/.mozilla/firefox/*.default*/cookies.sqlite")
    cookies = []
    for db in glob.glob(pattern_c):
        td = tempfile.mkdtemp()
        tmp = os.path.join(td, "cookies.sqlite")
        shutil.copy(db, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute("SELECT host, name, value, path, isSecure, isHttpOnly FROM moz_cookies WHERE host LIKE '%qwen%'").fetchall()
        for host, name, val, path, sec, http_only in rows:
            cookies.append({
                "name": name,
                "value": val,
                "domain": host,
                "path": path or "/",
                "secure": bool(sec),
                "httpOnly": bool(http_only)
            })
        con.close()
        shutil.rmtree(td)

    # 2. LocalStorage
    pattern_ls = os.path.expanduser("~/snap/firefox/common/.mozilla/firefox/*.default*/storage/default/https+++chat.qwen.ai/ls/data.sqlite")
    ls_data = {}
    for db in glob.glob(pattern_ls):
        td = tempfile.mkdtemp()
        tmp = os.path.join(td, "data.sqlite")
        shutil.copy(db, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute("SELECT key, value FROM data").fetchall()
        for k, v in rows:
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            ls_data[k] = v
        con.close()
        shutil.rmtree(td)

    return {"cookies": cookies, "ls": ls_data}

if __name__ == "__main__":
    print(json.dumps(extract()))
