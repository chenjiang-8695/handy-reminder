import re
import pathlib

BT = chr(96)
src = pathlib.Path("docs/使用说明-免安装版.md").read_text(encoding="utf-8")
out = []
for line in src.splitlines():
    s = line.rstrip()
    if s.startswith("# "):
        out += [s[2:].strip(), "=" * 40]
    elif s.startswith("## "):
        out += ["", "■ " + s[3:].strip(), "-" * 30]
    elif s.startswith("|"):
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        cells = [c.replace("**", "").replace(BT, "") for c in cells]
        if len(cells) == 2:
            if cells[0] in ("想做什么", "步骤", "改动"):
                out.append("  " + cells[0] + "  ->  " + cells[1])
            else:
                out.append("  " + cells[0] + "  ->  " + cells[1])
        else:
            out.append("  " + " | ".join(cells))
    else:
        out.append(s.replace("**", "").replace(BT, ""))

txt = "\r\n".join(out)
while "\r\n\r\n\r\n" in txt:
    txt = txt.replace("\r\n\r\n\r\n", "\r\n\r\n")
target = pathlib.Path("dist/HandyReminder/使用说明.txt")
target.write_text(txt, encoding="utf-8-sig")
print("ok", len(txt), "chars; backticks left:", txt.count(BT))
