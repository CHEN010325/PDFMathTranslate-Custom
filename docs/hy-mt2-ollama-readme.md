# Hy-MT2 — 腾讯混元翻译模型(Ollama 打包)

腾讯 **Hunyuan-MT2 / Hy-MT2** 系列翻译模型的 Ollama 打包版。GGUF 源文件直接取自腾讯官方 HuggingFace 仓库(**非第三方转换**),已内置官方对话模板与推荐参数,拉取即用,无需任何配置。

> 出处:[tencent/Hy-MT2-7B-GGUF](https://huggingface.co/tencent/Hy-MT2-7B-GGUF) · [tencent/Hy-MT2-1.8B-GGUF](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF)(Hugging Face 官方仓库)

## 拉取

```bash
ollama pull s2021008840/hy-mt2            # 7B Q4_K_M(推荐,质量/速度平衡)
ollama pull s2021008840/hy-mt2:1.8b       # 1.8B 轻量版(显存小/速度快)
ollama pull s2021008840/hy-mt2:7b-q8_0    # 7B 近无损
```

## 标签

| 标签 | 大小 | 说明 |
|---|---|---|
| `latest` `7b` `7b-q4_k_m` | 4.6 GB | 7B,Q4_K_M 量化 |
| `7b-q6_k` | 6.2 GB | 7B,Q6_K |
| `7b-q8_0` | 8.0 GB | 7B,Q8_0(近无损) |
| `1.8b` `1.8b-q4_k_m` | 1.1 GB | 1.8B,Q4_K_M |
| `1.8b-q6_k` | 1.5 GB | 1.8B,Q6_K |
| `1.8b-q8_0` | 1.9 GB | 1.8B,Q8_0 |

> 2bit / 1.25bit 极低比特实验格式暂未收录(当前 Ollama 的 GGUF 解析器尚不支持该张量布局)。

## 支持语言(33 语种 + 变体)

中、英、法、葡、西、日、土、俄、阿、韩、泰、意、德、越、马来、印尼、菲律宾、印地、繁体中文、波兰、捷克、荷兰、高棉、缅甸、波斯、古吉拉特、乌尔都、泰卢固、马拉地、希伯来、孟加拉、泰米尔、乌克兰、藏语、哈萨克、蒙古、维吾尔、粤语

## 用法

```bash
ollama run s2021008840/hy-mt2 "把下面的文本翻译成简体中文，不要额外解释。

Your text here..."
```

提示词按官方风格书写:目标语言用**完整语言名**(如“翻译成简体中文”),不需要 system prompt。

## 推荐参数

已在模板中内置,如需自行调整:

| 参数 | 值 |
|---|---|
| temperature | 0.7 |
| top_p | 0.6 |
| top_k | 20 |
| repeat_penalty | 1.05 |

## 注意事项

- 该系列 GGUF 使用腾讯 **STQ 量化内核**,需要较新的推理端:Ollama ≥ 0.34.4 实测正常;老版本 llama.cpp 需含 PR #22836。
- 上下文长度 256K,输入为纯文本。
- 本打包仅聚合 1.8B / 7B 的官方 GGUF;30B-A3B(MoE)系列官方未发布 GGUF,故未收录。

## 许可

TENCENT HY COMMUNITY LICENSE(版权归腾讯所有,本仓库仅为官方权重的格式转换打包)。
