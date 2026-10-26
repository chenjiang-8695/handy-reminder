# 网页版式样本: 宽画布、字号偏小、行距松、空格与标点多 (模拟网页正文)
BASE_NAME = "web"
fs = (14, 22)
LAYOUT = dict(
    IMAGE_WIDTH=(900, 1400),
    IMAGE_HEIGHT=(360, 900),
    FONT_SIZE=fs,
    CHAR_SPACING=[0, 0, 0, 0, 1],
    LINE_SPACING=(int(1 / 2 * fs[0]), int(1.2 * fs[0])),
)
CONTEXT = dict(
    HANZI_PROPORTION=-1,
    LETTER_PROPORTION=(0, 0.08),
    NUMBER_PROPORTION=(0, 0.08),
    PUNCTUATION_PROPORTION=(0.02, 0.08),
    SPACE_PROPORTION=(0.01, 0.05),
)
CORPUS_PATH = r"C:/Users/26756/Desktop/lingling/reminder-assistant/tools/ocr-samples/corpus-times.txt"
SAVE_RECTIFIED_LINES_SEPARATELY = False
