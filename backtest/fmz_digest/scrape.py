"""抓取 https://www.fmz.com/digest 的文章列表与正文（含代码块），缓存到 .cache/digest。"""
import html, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
import requests

BASE = "https://www.fmz.com"
CACHE = os.path.join(os.path.dirname(__file__), "..", ".cache", "digest")
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"


def get(url):
    for _ in range(3):
        try:
            r = S.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
        except requests.RequestException:
            pass
    return ""


def list_page(p):
    t = get(f"{BASE}/digest/{p}" if p > 1 else f"{BASE}/digest")
    out = {}
    for tid, a in re.findall(r'href="/digest-topic/(\d+)"[^>]*>(.*?)</a>', t, re.S):
        s = html.unescape(re.sub(r"<[^>]+>|\.fmz-[^{]+\{[^}]*\}", "", a)).strip()
        if s:
            out[int(tid)] = s
    return out


def article(tid):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{tid}.json")
    if os.path.exists(path):
        return json.load(open(path))
    t = get(f"{BASE}/digest-topic/{tid}")
    codes = [html.unescape(re.sub(r"<[^>]+>", "", c)) for c in re.findall(r"<pre[^>]*>(.*?)</pre>", t, re.S)]
    body = html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", t, flags=re.S))
    d = {"id": tid, "text": re.sub(r"\s+", " ", body)[:20000], "codes": codes}
    json.dump(d, open(path, "w"), ensure_ascii=False)
    return d


def all_titles(max_pages=1000):
    titles, p = {}, 1
    with ThreadPoolExecutor(16) as ex:
        while p <= max_pages:
            batch = list(ex.map(list_page, range(p, p + 16)))
            for b in batch:
                titles.update(b)
            if not any(batch):
                break
            p += 16
    return titles


if __name__ == "__main__":
    titles = all_titles(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
    os.makedirs(CACHE, exist_ok=True)
    json.dump(titles, open(os.path.join(CACHE, "titles.json"), "w"), ensure_ascii=False, indent=0)
    print(len(titles), "articles")
