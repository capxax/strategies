"""扫描仓库里的策略 .md，按语言和关键词归入 strategies.py 的策略族，统计覆盖率。

python catalog.py            -> reports/catalog.csv + 覆盖统计
"""
import glob, os, re
import pandas as pd
from strategies import REGISTRY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse(path):
    txt = open(path, encoding="utf-8", errors="ignore").read()
    name = re.search(r"> Name\s+(.+)", txt)
    lang = re.search(r"> Source \((\w+)\)", txt)
    desc = txt.split("> Source")[0].lower()
    title = (name.group(1) if name else os.path.basename(path)).lower()
    # 标题权重最高，其次正文描述
    scores = {}
    for fam, spec in REGISTRY.items():
        s = sum(3 * title.count(k.lower()) + min(desc.count(k.lower()), 3) for k in spec["keywords"])
        if s:
            scores[fam] = s
    best = max(scores, key=scores.get) if scores else "unmapped"
    return {"file": os.path.basename(path), "name": name.group(1).strip() if name else "",
            "lang": lang.group(1) if lang else "none", "family": best}


def main():
    files = [f for f in glob.glob(os.path.join(ROOT, "*.md")) if not f.endswith("README.md")]
    df = pd.DataFrame(parse(f) for f in files)
    os.makedirs("reports", exist_ok=True)
    df.to_csv("reports/catalog.csv", index=False)
    print(f"共 {len(df)} 个策略\n\n按语言:\n{df.lang.value_counts().to_string()}\n")
    print(f"按策略族（可用 run.py 回测的代表实现）:\n{df.family.value_counts().to_string()}")
    print(f"\n覆盖率: {(df.family != 'unmapped').mean():.1%}")


if __name__ == "__main__":
    main()
