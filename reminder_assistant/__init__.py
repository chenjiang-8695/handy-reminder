"""智能手账式悬浮提醒助手 —— 参考实现 (core, 无第三方依赖)。

模块划分:
    timeparse.py  中文自然语言时间解析
    models.py     领域模型 (Reminder / Occurrence / ReminderPlan)
    clock.py      时钟抽象 (可注入, 便于测试与漂移检测)
    store.py      SQLite 持久化 (WAL + 幂等 + 多实例锁)
    scheduler.py  单线程堆调度器 (漂移/休眠/改时间补偿)
    lifecycle.py  事件生命周期 (完成 / 过期静默清理 / 归档)
    notify.py     通知抽象 (优先级 / 去重 / 免打扰降级)
    ocr.py        圈选截图 + OCR + 时间语义抽取
    hotkey.py     全局快捷键 (Win32 RegisterHotKey)
    ui/           PySide6 便签悬浮窗 + 仿真翻页
    app.py        组装与启动
"""

__version__ = "2.6.0"
