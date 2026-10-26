# 句子型 OCR 样本：怎么生成、怎么评估

`Mactarvish/ocr-sample-generator` 只会画**随机汉字串**，测不出 PRD 4.2 的"时间语义提取"。
这里给它补了一个**句子模式**（补丁 + 语料 + 评估脚本），用来产出"真实句子 + 真实时间表达"的样本。

## 目录

| 文件 | 作用 |
|---|---|
| `patches/sentence-mode.patch` | 给生成器打的两处补丁（只动 `core/` 与 `main.py`，纯文本） |
| `corpus-times.txt` | 61 句语料：能解析的时间 / 非法时间 / 完全没时间的句子 |
| `config-sentences.py` | 句子模式配置（一行一句、字号 18~26、轻量背景与噪声） |
| `eval_samples.py` | 把生成结果喂给我们自己的 `ocr.evaluate()`（按 y1 排序拼真值） |

## 补丁做了什么

1. `core/ocr_sample_generator.py` 新增 `put_sentence_line(text, tl_y)` —— 把**指定句子**画成一行，
   其余链路（字体切换、字符间距、标注、增强、切行）完全复用原实现。
2. `core/*` 的中文字体判定从"5883 字表 + 中文标点"改成**按 Unicode 区段**判断
   （`is_cjk`）。不改的话，语料里表外常用字（周/早/半/货…）会落到拉丁字体上渲染成豆腐块。
3. `main.py` 支持 `SENTENCES` / `CORPUS_PATH`：配置里有句子就用句子模式，一行一句；
   每张图内**不重复**使用同一句（重复句会让字符准确率虚高）。

## 一次性准备（新机器）

```bash
git clone --depth 1 https://github.com/Mactarvish/ocr-sample-generator.git
cd ocr-sample-generator
git apply <本仓库>/tools/ocr-samples/patches/sentence-mode.patch

# 一次性依赖：仓库的 requirements.txt 是 2020 年的，直接装会失败
python -m venv venv && venv/Scripts/pip install numpy<2 "opencv-python<4.10" imgaug tqdm "pillow<10" matplotlib

# 字体：仓库自带 SimSun / 楷体_GB2312 / Times 都是专有字体，删掉换成 OFL 的
rm assets/fonts/cn/*.ttf assets/fonts/en/*.ttf
cp <本仓库>/reminder_assistant/assets/fonts/LXGWWenKai-Medium.ttf assets/fonts/cn/
cp <matplotlib>/mpl-data/fonts/ttf/DejaVuSans.ttf assets/fonts/en/
```

## 生成

```bash
# 参数: 配置 输出目录 张数
venv/Scripts/python.exe main.py <本仓库>/tools/ocr-samples/config-sentences.py C:/tmp/sent 60
```

产物：`global_canvas/{images,labels,visualization}` + `rectified_lines/{images,labels}`。
每张整页约 11 行，60 张≈650 句。

## 评估

```bash
python tools/ocr-samples/eval_samples.py C:/tmp/sent                  # 整页（贴近"圈一块屏幕区域"）
python tools/ocr-samples/eval_samples.py C:/tmp/sent --sub rectified_lines   # 单行（crop 质量差, 偏低）
```

三个数字的含义：

| 指标 | 含义 |
|---|---|
| `char_accuracy` | 字符准确率（1 − 编辑距离/长度；空白不计错） |
| `time_extract_accuracy` | 图里有时间，识别文本里**也解析出了**时间 |
| `time_value_accuracy` | 解析出的时刻和图上写的**一致**（精确到分钟） |

## 2026-10-26 实测（RapidOcrEngine，离线）

| 样本 | char_accuracy | time_extract | time_value |
|---|---|---|---|
| 60 张整页（≈650 句，霞鹜文楷 + 随机背景/轻噪/旋转） | **86.8%** | **100%** | **93.3%** |
| 24 张整页（同一配置的早期批次） | 88.5% | 100% | 87.5% |
| 259 张单行 crop | 69.7% | 71.0% | 69.6% |
| 10 张随机汉字串（对照） | 78.2% | — | — |

怎么读这几个数：

- **整页那两行才是产品口径**：用户圈的是屏幕上的一块区域（多行），不是被透视撕过的细条。
- **单行 crop 偏低是数据问题不是识别问题**：切行后的细条分辨率差、有的几乎是空的，
  换成真实截图不会这样。要按行评估，得用真实截图而不是生成器的 crop。
- 24 张那批 87.5% 的差额，一半来自"一张图里多个时间点、第一句取的是哪个"——
  所以评估脚本改成按 y1 排序拼真值，60 张这批升到 93.3%。

## 还差什么

PRD 4.2 要 100~200 张、覆盖网页/PDF/聊天截图/设计图/手写。生成器只能覆盖**印刷体**：

- 印刷体：用本工具生成 120 张（网页/PDF/表格三种版式，改 `LAYOUT`/`CONTEXT` 组合）
- 聊天截图 / 设计图：用真实截图（`桌面/lingling/测试图片/`、`_archive/报告截图/`）
- **手写**：必须真实采集；霞鹜文楷生成的"像手写"不能替代手写识别验收
