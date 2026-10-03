"""逐句比对:检测纯译文 PDF 中原样残留的英文长句(漏翻信号)。

用法: venv python script/leak_check.py
输出: 每篇文档的长句总数 / 原样残留数 + 残留样本,写入 %TEMP%/leak_check.json
"""
import pymupdf, re, glob, os, json, tempfile

EXPORT = r"C:\Users\chenshulin\Downloads\PDFMathTranslate-Custom\pdf2zh\kernel\PDFMathTranslate-next.git\pdf2zh_files\_exports"
UPLOAD = r"C:\Users\chenshulin\Downloads\PDFMathTranslate-Custom\pdf2zh\kernel\PDFMathTranslate-next.git\pdf2zh_files\_uploads"
SKIP_RE = re.compile(r"https?://|doi\.org|www\.|@")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def main() -> None:
    results = {}
    for mp in sorted(glob.glob(os.path.join(EXPORT, "*-纯译文.pdf"))):
        name = os.path.basename(mp)[: -len("-纯译文.pdf")]
        orig_path = os.path.join(UPLOAD, name + ".pdf")
        if not os.path.exists(orig_path):
            print(f"[skip] 找不到原文: {name}")
            continue
        odoc, mdoc = pymupdf.open(orig_path), pymupdf.open(mp)
        otext = norm(" ".join(pg.get_text() for pg in odoc))
        mtext = norm(" ".join(pg.get_text() for pg in mdoc))
        total = hits = 0
        hit_list = []
        for sent in re.split(r"(?<=[.!?])\s+", otext):
            words = re.findall(r"[A-Za-z]{2,}", sent)
            if len(words) < 10:
                continue
            if SKIP_RE.search(sent) or "{" in sent or "}" in sent:
                continue
            total += 1
            s = norm(sent)[:180]
            if s in mtext:
                hits += 1
                hit_list.append(sent.strip())
        results[name] = {"句子总数": total, "原样残留": hits, "样本": hit_list[:4]}
        print(f"{name[:30]:<32} 长句 {total:>4} | 原样残留 {hits:>3}")
        odoc.close()
        mdoc.close()

    out = os.path.join(tempfile.gettempdir(), "leak_check.json")
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== 残留样本(每篇前4条)===")
    for name, r in results.items():
        for s in r["样本"]:
            print(f"[{name[:16]}] {s[:110]}")
    print("\nJSON:", out)


if __name__ == "__main__":
    main()
