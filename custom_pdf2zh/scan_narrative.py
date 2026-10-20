"""Translate complete scan paragraphs with checked, opaque technical spans."""

from collections import Counter
import re

ATOM = re.compile(
    r"\$+.*?\$+|(?:ISO|CEN/TS)\s*[\d:-]+(?:\[\d+\])?"
    r"|[A-Za-zσπθΔ]+(?:[_^]\([^()]*\))+"
    r"|\b(?:MFR|PFA|SDR|PE(?:\s*\d+)?)\b"
    r"|\d+(?:[.,]\d+)*(?:\s*(?:°\s*C|GJ/m[²³23]|kg/m[²³23]|mm|kg|mg|bar|MPa|%))?"
    r"|[≤≥<>±×÷=+−]",
    re.DOTALL,
)
MARKER = re.compile(r"\{v\d+\}")


class ScanTerminology:
    """Narrow document-domain terminology; never installed on native translation."""

    def __init__(self, translator, domain, lang_out):
        self.translator, self.domain, self.lang_out = translator, domain, lang_out

    def __getattr__(self, name):
        return getattr(self.translator, name)

    def translate(self, text, *args, **kwargs):
        result = self.translator.translate(text, *args, **kwargs)
        if self.domain != "polyethylene-piping" or not self.lang_out.startswith("zh"):
            return result
        if re.search(r"\bcompounds?\b", text, re.I):
            result = re.sub(r"化合物|复合材料|复合物|混合物", "混配料", result)
        if re.search(r"fusion compatibility", text, re.I):
            result = re.sub(
                r"融合兼容性|熔合兼容性|熔接兼容性|融合相容性", "熔接相容性", result
            )
        return result


def split_footnotes(text):
    matches = list(re.finditer(r"(?:^|(?<=[.!?]))\s*([a-i])\s*(?=[A-Z])", text))
    labels = [m[1] for m in matches]
    if len(labels) < 2 or labels != list("abcdefghi"[: len(labels)]):
        return []
    return [
        {
            "label": m[1],
            "text": text[
                m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ].strip(),
        }
        for i, m in enumerate(matches)
    ]


def translate_paragraph(translator, text, lang_out, cancel_check=None):
    def translate(source):
        if cancel_check:
            cancel_check()
        result = translator.translate(source).strip()
        if cancel_check:
            cancel_check()
        return result

    if MARKER.search(text):
        raise RuntimeError("扫描原文包含保留占位符，无法安全保护技术内容")
    spans = []
    for match in ATOM.finditer(text):
        if spans and re.fullmatch(r"[\s,+/()-]*", text[spans[-1][1] : match.start()]):
            spans[-1] = (spans[-1][0], match.end())
        else:
            spans.append((match.start(), match.end()))
    atoms = [text[start:end] for start, end in spans]
    masked, offset = [], 0
    for i, (start, end) in enumerate(spans):
        masked.extend([text[offset:start], "{v%d}" % i])
        offset = end
    masked.append(text[offset:])
    masked = "".join(masked)
    translated = translate(masked)
    expected = Counter("{v%d}" % i for i in range(len(atoms)))

    def valid(source, result):
        return (
            bool(result.strip())
            and Counter(MARKER.findall(result)) == Counter(MARKER.findall(source))
            and not re.search(r"\d|[≤≥<>±×÷=+−]", MARKER.sub("", result))
        )

    if not valid(masked, translated):
        # Long multi-condition notes can make a small model omit a repeated
        # symbol. Retry complete sentences/alternative conditions, never isolated
        # prose islands around values. Every clause has the same exact contract.
        clauses = re.split(
            r"(?<=[.!?;])\s*(?=[A-Z{])|(?<=,)\s+(?=or\s+\{v\d+\})", masked
        )
        if len(clauses) > 1:
            retried = []
            for clause in clauses:
                result = translate(clause)
                if not valid(clause, result):
                    raise RuntimeError("扫描段落的技术内容保护校验失败，已停止导出")
                retried.append(result)
            translated = "\n".join(retried)
    if not translated or Counter(MARKER.findall(translated)) != expected:
        raise RuntimeError("扫描段落的技术内容保护校验失败，已停止导出")
    # A model may echo all markers while adding an unrequested number.
    if re.search(r"\d|[≤≥<>±×÷=+−]", MARKER.sub("", translated)):
        raise RuntimeError("扫描段落出现新增数值或运算符，已停止导出")
    return MARKER.sub(lambda m: atoms[int(m[0][2:-1])], translated)
