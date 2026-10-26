把内置手写字体放在这个目录, 启动时会自动注册 (theme.load_bundled_fonts)。

## 现在放的是什么

LXGWWenKai-Medium.ttf —— 霞鹜文楷 Medium v1.530 (2026-10-18), SIL Open Font License 1.1。
来源: https://github.com/lxgw/LxgwWenKai/releases

| 检查项 | 结果 |
|---|---|
| 字形数 | 47,336 |
| CJK 基本区 | 20,992 字（+ 扩展 8,562 字） |
| 中日韩标点 / 全角 | 285 |
| 对 bundle 语料（1291 常用字）覆盖 | 100% |
| 嵌入许可 fsType | 0（可安装 / 可嵌入） |
| Qt 里注册到的家族名 | `LXGW WenKai` |

**关于 Bold**: PRD 与旧文档写的是 `LXGWWenKai-Bold.ttf`，但上游 v1.530 的 release 里
只有 Light / Medium / Regular 三档，**没有 Bold**。这里放 Medium 作为最重的一档，
加粗由 Qt 合成（`QFont.setBold(True)`）。想换 Regular 直接替换同名文件即可。

## 为什么不能用别的字体

Chewy（google/fonts 里的 Apache-2.0 花体）验证过：只有 229 个字形、**CJK 汉字 0 个**，
中文完全落空 —— 它只能给拉丁字符用，不能当这套中文便签的正文。

备选（都开源可商用）: 悠哉字体 Yozai / 霞鹜新晰黑 / 思源黑体 Noto Sans SC。

放好之后 PRD 4.1 "内置的加粗手写字体在主流分辨率下 100% 清晰可辨认" 才有保障 ——
否则程序会退回系统楷体，不同机器上字迹不一致。
