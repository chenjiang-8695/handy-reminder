# 表格/密集版式样本: 窄行距、小字号、行多 (模拟表格、聊天记录、清单)
BASE_NAME = "table"
fs = (13, 18)
LAYOUT = dict(
    IMAGE_WIDTH=(520, 900),
    IMAGE_HEIGHT=(420, 800),
    FONT_SIZE=fs,
    CHAR_SPACING=[0, 0, 0, 1],
    LINE_SPACING=(2, int(0.4 * fs[0])),
)
CONTEXT = dict(
    HANZI_PROPORTION=-1,
    LETTER_PROPORTION=(0, 0.05),
    NUMBER_PROPORTION=(0.01, 0.06),
    PUNCTUATION_PROPORTION=(0, 0.04),
    SPACE_PROPORTION=(0, 0.03),
)
CORPUS_PATH = r"C:/Users/26756/Desktop/lingling/reminder-assistant/tools/ocr-samples/corpus-times.txt"
SAVE_RECTIFIED_LINES_SEPARATELY = False
