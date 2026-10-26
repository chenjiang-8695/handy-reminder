# PDF 版式样本: 中等画布、字号偏大、行距中等 (模拟 PDF 正文/公文)
BASE_NAME = "pdf"
fs = (20, 30)
LAYOUT = dict(
    IMAGE_WIDTH=(700, 1100),
    IMAGE_HEIGHT=(600, 1200),
    FONT_SIZE=fs,
    CHAR_SPACING=[0, 0, 1, 2],
    LINE_SPACING=(int(1 / 3 * fs[0]), int(0.9 * fs[0])),
)
CONTEXT = dict(
    HANZI_PROPORTION=-1,
    LETTER_PROPORTION=(0, 0.05),
    NUMBER_PROPORTION=(0, 0.05),
    PUNCTUATION_PROPORTION=(0.01, 0.05),
    SPACE_PROPORTION=(0, 0.02),
)
CORPUS_PATH = r"C:/Users/26756/Desktop/lingling/reminder-assistant/tools/ocr-samples/corpus-times.txt"
SAVE_RECTIFIED_LINES_SEPARATELY = False
