"""Small inline-notation bridge; display formulas retain their source image."""

import html
import re


def inline_math(text):
    commands = {
        "pm": "±",
        "leq": "≤",
        "le": "≤",
        "geq": "≥",
        "ge": "≥",
        "times": "×",
        "cdot": "·",
        "sigma": "σ",
        "pi": "π",
        "theta": "θ",
        "Delta": "Δ",
        "circ": "°",
        "min": "min",
        "max": "max",
    }

    def convert(match):
        value = match.group(1).strip()
        for _ in range(6):
            value = re.sub(
                r"\\(?:mathrm|text|mathbf|mathit|underline)\{([^{}]*)\}", r"\1", value
            )
        for name in sorted(commands, key=len, reverse=True):
            value = re.sub(r"\\" + name + r"(?![A-Za-z])", commands[name], value)
        value = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", value)
        value = re.sub(r"\^\{°\}", "°", value)
        value = re.sub(r"([_^])\{([^{}]*)\}", r"\1(\2)", value)
        value = re.sub(r"([_^])([A-Za-z0-9])", r"\1(\2)", value)
        value = re.sub(r"\\[,;! ]", " ", value)
        value = value.replace(r"\%", "%")
        if re.search(r"\\[A-Za-z]", value):
            raise RuntimeError("扫描件包含尚不支持的行内公式，已停止导出")
        return value.strip()

    return re.sub(r"\$+(.*?)\$+", convert, text, flags=re.DOTALL)


def rich_text(text):
    text = html.escape(inline_math(text))
    text = re.sub(r"_\(([^()]*)\)", r"<sub>\1</sub>", text)
    text = re.sub(r"\^\(([^()]*)\)", r"<sup>\1</sup>", text)
    return text.replace("\n", "<br>")
