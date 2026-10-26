# 句子模式配置: 一行一句真实语料, 用来测"时间语义提取"(随机汉字串测不了这个)
# 用法:
#   python main.py <这个文件> <输出目录> <张数>
# 语料里刻意混进了 3 类:
#   1. 能解析的时间(中文数字/阿拉伯数字/日期/相对时间/星期)
#   2. 非法时间("明天25点"、"下午2点60分") —— 应当**不**被解析成提醒
#   3. 完全没有时间的句子 —— 应当不建卡
BASE_NAME = "sent"

fs = (18, 26)
LAYOUT = dict(
    IMAGE_WIDTH=(520, 900),
    IMAGE_HEIGHT=(180, 520),
    FONT_SIZE=fs,
    CHAR_SPACING=[0, 0, 0, 1, 2],
    LINE_SPACING=(int(1 / 4 * fs[0]), int(1 / 2 * fs[0])),
)

CONTEXT = dict(
    HANZI_PROPORTION=-1,
    LETTER_PROPORTION=(0, 0.05),
    NUMBER_PROPORTION=(0, 0.05),
    PUNCTUATION_PROPORTION=(0, 0.05),
    SPACE_PROPORTION=(0, 0.02),
)

CORPUS_PATH = r"C:/Users/26756/Desktop/lingling/reminder-assistant/tools/ocr-samples/corpus-times.txt"
SAVE_RECTIFIED_LINES_SEPARATELY = True
