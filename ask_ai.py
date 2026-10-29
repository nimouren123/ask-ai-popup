#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ask AI Popup — 划词即问

在任何程序里选中一段文字，按 Ctrl+Alt+Space，旁边弹出一个小窗把它讲透。
它会自动去找这段文字出自哪条 AI 回复，把整段连同前后文一起塞进 prompt ——
所以你问「这一步的函数」时，它知道你说的是哪一步。

依赖: pip install -r requirements.txt

用法:
    python ask_ai.py            启动
    python ask_ai.py --test     自检（不弹窗，验证 key + 剪贴板）

    Ctrl+Alt+Space     问
    Ctrl+Alt+Shift+S   设置
    Ctrl+Alt+Shift+X   退出

环境变量:
    ASK_AI_API_KEY     API key（不给的话去 ~/.claude/settings.json 里找）
    ASK_AI_BASE_URL    默认 https://api.deepseek.com
    ASK_AI_MODEL       默认 deepseek-chat
"""
import os
import sys
import json
import time
import html
import ctypes
import threading
import glob
import re
from ctypes import wintypes

import pyperclip
from openai import OpenAI

if sys.stdout is None:            # pythonw 启动没有控制台，print 会直接炸
    sys.stdout = open(os.devnull, "w")

from PyQt6.QtCore import (Qt, QObject, QRectF, pyqtSignal,
                          QPropertyAnimation, QEasingCurve)
from PyQt6.QtGui import (QPainter, QPainterPath, QColor, QPixmap, QTextCursor,
                         QFont, QLinearGradient, QPen, QBrush,
                         QTextBlockFormat, QImage)
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QTextBrowser, QLineEdit, QPushButton,
                             QDialog, QFormLayout, QComboBox, QSpinBox,
                             QPlainTextEdit, QColorDialog, QFileDialog,
                             QMessageBox, QSlider, QDialogButtonBox, QFrame)

# ── 配置 ──────────────────────────────────────────
def _load_key():
    """没设环境变量时，从 Claude Code 的配置里捞 key —— 装了它的话省得再配一遍"""
    for p in (os.path.expanduser("~/.claude/settings.json"),
              os.path.expanduser("~/.claude/settings.local.json")):
        try:
            k = json.load(open(p, encoding="utf-8")).get("env", {}).get("ANTHROPIC_AUTH_TOKEN")
            if k:
                return k
        except Exception:
            pass
    return ""


API_KEY  = (os.getenv("ASK_AI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
            or _load_key())
BASE_URL = os.getenv("ASK_AI_BASE_URL") or "https://api.deepseek.com"
MODEL    = os.getenv("ASK_AI_MODEL") or "deepseek-chat"

SYSTEM_PROMPT = '用户在学一个东西，从一大段讲解里选中了一小块来问。\n你的任务：把这一小块讲透，不要重新讲整套内容。\n1. 一句话说清这段是做什么的\n2. 逐行 / 逐参数解释\n3. 指出容易踩的坑\n4. 涉及数据结构就说明 shape\n如果背景里提到过它，点到为止地引用一句即可，不要复述前文。\n用中文，简洁，直接上干货。'

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ask_ai_config.json")

_DEFAULTS = {
    "bg":        "#ffffff",
    "fg":        "#16181d",
    "accent":    "#2f6feb",
    "font":      "宋体",
    "font_size": 11,
    "bg_image":  "",          # 背景图路径，空 = 不用图
    "scrim":     30,          # 图上面压多厚的一层底色（%），越高字越清楚
    "blur":      0,           # 背景图高斯模糊半径，0 = 原图
    "ctx_scan":  60,
    "ctx_chars": 1500,
    "ctx_users": 5,
    "prompt":    SYSTEM_PROMPT,
}

PRESETS = {
    "浅色":   ("#ffffff", "#16181d", "#2f6feb"),
    "深色":   ("#17181a", "#e6e6e6", "#7aa2f7"),
    "米黄":   ("#faf6ec", "#33302a", "#a05a2c"),
    "护眼绿": ("#c7edcc", "#22301f", "#2f6b3a"),
}

FONTS = {"宋体": "SimSun", "黑体": "SimHei", "雅黑": "Microsoft YaHei UI"}


def _load_cfg():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return dict(_DEFAULTS)
    out = {**_DEFAULTS, **c}
    for k in ("ctx_scan", "ctx_chars", "ctx_users", "font_size", "scrim", "blur"):
        try:
            out[k] = int(out[k])
        except (TypeError, ValueError):
            out[k] = _DEFAULTS[k]
    return out


CFG = _load_cfg()


def _save_cfg():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        QMessageBox.critical(None, "保存失败", str(e))
        return False


def _mix(c1, c2, t=0.5):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _surface():
    return _mix(CFG["bg"], CFG["fg"], 0.08)


def _muted():
    return _mix(CFG["fg"], CFG["bg"], 0.28)


def _accent():
    return CFG.get("accent") or "#2f6feb"


def _font(delta=0):
    return QFont(FONTS.get(CFG["font"], "SimSun"), max(7, CFG["font_size"] + delta))


# ── 上下文：从 Claude Code 最近会话里取 ──────────
def _load_context(selected=""):
    """按【位置】取上下文：定位选中文字来自哪条 AI 回复，取它 + 前后各一条。

    场景：AI 给了一整套分步讲解，用户翻回去抠其中一小块来问。
    那块代码脱离所属的那一整条回复就看不懂 —— 所以按位置取，
    比「取最近几条」准得多。定位失败才退回最近两条。
    """
    max_scan  = int(CFG["ctx_scan"])
    seg_chars = int(CFG["ctx_chars"])
    max_users = int(CFG["ctx_users"])
    if max_scan <= 0:
        return ""

    base = os.path.expanduser("~/.claude/projects")
    files = glob.glob(os.path.join(base, "*", "*.jsonl"))
    if not files:
        return ""
    newest = max(files, key=os.path.getmtime)
    try:
        with open(newest, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 3000000))
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

    assists, users = [], []
    for line in reversed(raw.strip().splitlines()[1:]):
        try:
            o = json.loads(line)
        except Exception:
            continue
        kind = o.get("type")
        if kind not in ("user", "assistant") or o.get("isMeta"):
            continue
        c = o.get("message", {}).get("content")
        if isinstance(c, str):
            t = c.strip()
        elif isinstance(c, list):
            t = " ".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        else:
            t = ""
        if not t or t[:1] in ("<", "["):
            continue
        if kind == "assistant":
            if len(assists) < max_scan:
                assists.append(t)
        elif len(users) < max_users:
            users.append(t[:180])
        if len(assists) >= max_scan and len(users) >= max_users:
            break

    blocks = []
    hit = -1
    if selected and assists:
        key = re.sub(r"\s+", "", selected)[:40]
        if len(key) >= 6:          # 太短的选中（如一个字）容易误匹配
            for i, a in enumerate(assists):          # assists[0] 是最新的
                if key in re.sub(r"\s+", "", a):
                    hit = i
                    break

    if hit >= 0:
        lo, hi = max(0, hit - 1), min(len(assists), hit + 2)
        picked = list(reversed(assists[lo:hi]))
        blocks.append("=== 选中内容所在的讲解（前后各一段）===")
        for i, a in enumerate(picked):
            blocks.append("--- 第 %d 段 ---" % (i + 1))
            blocks.append(a[:seg_chars])
    elif assists:
        blocks.append("=== AI 最近的讲解 ===")
        for i, a in enumerate(reversed(assists[:2])):
            blocks.append("--- 第 %d 段 ---" % (i + 1))
            blocks.append(a[:seg_chars])

    if users:
        blocks.append("=== 用户最近问过 ===")
        blocks.extend("- " + u for u in reversed(users))

    return chr(10).join(blocks)


def _system_prompt(selected=""):
    ctx = _load_context(selected)
    base = CFG["prompt"]
    return base + chr(10) * 2 + ctx if ctx else base


# ── 抓选区 ────────────────────────────────────────
user32 = ctypes.windll.user32
MOD_ALT, MOD_CONTROL, MOD_SHIFT, WM_HOTKEY = 0x0001, 0x0002, 0x0004, 0x0312
VK_ASK, VK_SET, VK_QUIT = 0x20, 0x53, 0x58      # Space / S / X
ID_ASK, ID_SET, ID_QUIT = 1, 3, 2
KEYUP = 0x0002


def _send_ctrl_c():
    user32.keybd_event(0x11, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(0x43, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(0x43, 0, KEYUP, 0)
    user32.keybd_event(0x11, 0, KEYUP, 0)


def _wait_keys_released(timeout=1.0):
    """等用户松开 Ctrl / Alt

    热键触发时物理按键还按着，此时发 Ctrl+C 会被当成组合键的一部分，
    目标程序收到的不是复制指令 —— 这就是"明明选中了却读不到"的原因。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        ctrl = user32.GetAsyncKeyState(0x11) & 0x8000
        alt = user32.GetAsyncKeyState(0x12) & 0x8000
        shift = user32.GetAsyncKeyState(0x10) & 0x8000
        if not ctrl and not alt and not shift:
            return True
        time.sleep(0.02)
    return False


def _grab_selection():
    _wait_keys_released()
    time.sleep(0.05)
    pyperclip.copy("")
    time.sleep(0.06)
    _send_ctrl_c()
    time.sleep(0.25)
    return pyperclip.paste().strip()


# ── 背景图 ────────────────────────────────────────
_pix_cache = {}


def _load_scaled(path, w, h):
    """读图 → 缩到目标尺寸 → 高斯模糊

    1 亿像素的壁纸直接 QPixmap(path) 会撞上 Qt 的内存上限、返回 0×0，
    所以优先走 Pillow：它既能缩也能糊。没装 Pillow 才退回 Qt（不模糊）。
    """
    want_w, want_h = w * 2, h * 2
    blur = int(CFG.get("blur", 20))
    try:
        from PIL import Image, ImageFilter
        Image.MAX_IMAGE_PIXELS = 400_000_000        # 别让 1 亿像素触发炸弹警告
        im = Image.open(path).convert("RGB")
        im.thumbnail((want_w, want_h), Image.LANCZOS)
        if blur > 0:
            im = im.filter(ImageFilter.GaussianBlur(blur * im.width / float(want_w)))
        buf = im.tobytes("raw", "RGB")
        qi = QImage(buf, im.width, im.height, im.width * 3,
                    QImage.Format.Format_RGB888).copy()
        return QPixmap.fromImage(qi)
    except Exception:
        pass

    from PyQt6.QtGui import QImageReader
    from PyQt6.QtCore import QSize
    r = QImageReader(path)
    if r.canRead():
        sz = r.size()
        if not sz.isEmpty():
            r.setScaledSize(sz.scaled(QSize(want_w, want_h),
                                      Qt.AspectRatioMode.KeepAspectRatio))
        img = r.read()
        if not img.isNull():
            return QPixmap.fromImage(img)
    return None


def _bg_pixmap(w, h):
    """背景图 cover 裁切；没配图 / 文件没了 / 解不开都返回 None"""
    path = CFG.get("bg_image") or ""
    if not path or not os.path.exists(path):
        return None
    key = (path, w, h, CFG.get("blur"))
    if key in _pix_cache:
        return _pix_cache[key]
    pix = _load_scaled(path, w, h)
    if pix is None or pix.isNull():
        return None
    pix = pix.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                     Qt.TransformationMode.SmoothTransformation)
    pix = pix.copy((pix.width() - w) // 2, (pix.height() - h) // 2, w, h)
    _pix_cache.clear()
    _pix_cache[key] = pix
    return pix


# ── 渲染：纯文本 → HTML（代码块 + 常用 Markdown）──
def _inline(s):
    """行内：**粗体** 和 `代码`"""
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`\n]+)`",
               lambda m: "<code style='background:%s; border-radius:4px;"
                         " padding:0 3px; font-family:Consolas;'>%s</code>"
                         % (_surface(), m.group(1)), s)
    return s


def _block(seg):
    """行级：标题 / 列表 / 普通段落"""
    lines = []
    for ln in seg.split("\n"):
        t = ln.rstrip()
        m = re.match(r"^\s*[-*]\s+(.*)$", t)
        if m:
            lines.append("· " + _inline(m.group(1)))
        elif t.startswith("### "):
            lines.append("<b>" + _inline(t[4:]) + "</b>")
        elif t.startswith("## "):
            lines.append("<b>" + _inline(t[3:]) + "</b>")
        elif t.startswith("# "):
            lines.append("<b>" + _inline(t[2:]) + "</b>")
        else:
            lines.append(_inline(t))
    return "<br>".join(lines)


def _render(txt):
    out = []
    for i, seg in enumerate(txt.split("```")):
        if i % 2:
            lines = seg.split("\n")
            if lines and lines[0].strip() and " " not in lines[0].strip():
                lines = lines[1:]          # 去掉 ```python 的语言名
            out.append(
                "<pre style='background:%s; padding:10px 12px; margin:8px 0;"
                " border-radius:8px; font-family:Consolas; font-size:%dpt;"
                " white-space:pre-wrap;'>%s</pre>"
                % (_surface(), max(8, CFG["font_size"] - 1),
                   html.escape("\n".join(lines))))
        else:
            out.append(_block(seg))
    return "".join(out)


# ── 主窗口 ────────────────────────────────────────
PANEL_W, PANEL_H, MARGIN, RADIUS = 480, 560, 16, 16


class Popup(QWidget):
    def __init__(self, selected):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(PANEL_W + MARGIN * 2, PANEL_H + MARGIN * 2)
        self._drag = None
        self._busy = False
        self.selected = selected
        self.messages = [{"role": "system", "content": _system_prompt(selected)},
                         {"role": "user", "content": selected}]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN + 20, MARGIN + 18, MARGIN + 20, MARGIN + 16)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.head = QLabel(selected[:160] + ("…" if len(selected) > 160 else ""))
        self.head.setObjectName("head")          # 必须在 setStyleSheet 之前，否则样式不生效
        self.head.setWordWrap(True)
        self.head.setMaximumHeight(52)
        top.addWidget(self.head, 1)
        self.btn_set = QPushButton("设置")
        self.btn_set.setObjectName("flat")
        self.btn_set.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_set.clicked.connect(self.open_settings)
        top.addWidget(self.btn_set, 0, Qt.AlignmentFlag.AlignTop)

        self.btn_close = QPushButton("×")
        self.btn_close.setObjectName("close")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setFixedWidth(22)
        self.btn_close.setToolTip("关闭（Esc）")
        self.btn_close.clicked.connect(self.close)
        top.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)

        self.sep = QFrame()
        self.sep.setFixedHeight(1)
        lay.addWidget(self.sep)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(False)
        self.body.setFont(_font())
        self.body.viewport().setAutoFillBackground(False)
        lay.addWidget(self.body, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.entry = QLineEdit()
        self.entry.setFont(_font())
        self.entry.setPlaceholderText("追问…")
        self.entry.returnPressed.connect(self.send_followup)
        bottom.addWidget(self.entry, 1)
        self.btn_send = QPushButton("追问")
        self.btn_send.setObjectName("send")
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.clicked.connect(self.send_followup)
        bottom.addWidget(self.btn_send)
        lay.addLayout(bottom)

        self.apply_style()
        self._first = True
        self.body.setPlainText("思考中…")
        self._stream()

    # ── 外观 ──
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)

        # 投影：外扩几圈、每圈都很淡，叠出柔和边缘
        for i in range(9, 0, -1):
            sp = QPainterPath()
            sp.addRoundedRect(panel.adjusted(-i * 0.7, -i * 0.5, i * 0.7, i * 0.9),
                              RADIUS + i * 0.7, RADIUS + i * 0.7)
            p.fillPath(sp, QColor(0, 0, 0, 5))

        path = QPainterPath()
        path.addRoundedRect(panel, RADIUS, RADIUS)

        pix = _bg_pixmap(int(panel.width()), int(panel.height()))
        if pix is not None:
            p.setClipPath(path)
            p.drawPixmap(int(panel.x()), int(panel.y()), pix)
            # 渐变压暗：上方略薄、下方略厚，有纵深但不至于上半截发白
            a = int(255 * min(1.0, max(0, CFG["scrim"]) / 100))
            c = QColor(CFG["bg"])
            g = QLinearGradient(0, panel.y(), 0, panel.bottom())
            top_col = QColor(c)
            top_col.setAlpha(int(a * 0.97))
            bot_col = QColor(c)
            bot_col.setAlpha(min(255, int(a * 1.04)))
            g.setColorAt(0.0, top_col)
            g.setColorAt(1.0, bot_col)
            p.fillPath(path, QBrush(g))

            # 顶部再单独压一层：不管壁纸多花，摘录和按钮都得看得清
            tg = QLinearGradient(0, panel.y(), 0, panel.y() + 104)
            c0 = QColor(CFG["bg"])
            c0.setAlpha(78)
            c1 = QColor(CFG["bg"])
            c1.setAlpha(0)
            tg.setColorAt(0.0, c0)
            tg.setColorAt(1.0, c1)
            p.fillRect(QRectF(panel.x(), panel.y(), panel.width(), 104), QBrush(tg))
            p.setClipping(False)
        else:
            p.fillPath(path, QColor(CFG["bg"]))

        # 一道极淡的内描边，把面板从桌面里「抬」起来
        edge = QPainterPath()
        edge.addRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        r, gg, b = (int(CFG["fg"][i:i + 2], 16) for i in (1, 3, 5))
        p.setPen(QPen(QColor(r, gg, b, 30), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(edge)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.entry.setFocus()             # 打开就能直接打字追问，不用先点一下
        self.setWindowOpacity(0.0)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(150)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()

    def apply_style(self):
        fam = FONTS.get(CFG["font"], "SimSun")
        fg, surface, muted, accent = CFG["fg"], _surface(), _muted(), _accent()
        soft = _mix(fg, CFG["bg"], 0.09)      # 背景是图，次要文字不能太淡
        self.setStyleSheet(f"""
            QWidget {{ color: {fg}; font-family: "{fam}"; font-size: {CFG['font_size']}pt;
                       background: transparent; }}
            QLabel#head {{ color: {soft}; font-size: {max(8, CFG['font_size']-1)}pt; }}
            QTextBrowser {{ background: transparent; border: none; }}
            QLineEdit {{ background: {surface}; border: 1px solid {surface};
                         border-radius: 7px; padding: 5px 9px; color: {fg};
                         selection-background-color: {accent}; selection-color: {CFG['bg']}; }}
            QLineEdit:focus {{ border: 1px solid {accent}; }}
            QPushButton#send {{ color: {accent}; background: transparent; border: none;
                                font-weight: bold; padding: 4px 6px; }}
            QPushButton#send:disabled {{ color: {muted}; }}
            QPushButton#flat {{ color: {_mix(fg, CFG['bg'], 0.35)}; background: transparent;
                                border: none; padding: 2px 4px; }}
            QPushButton#flat:hover {{ color: {accent}; }}
            QPushButton#close {{ color: {_mix(fg, CFG['bg'], 0.35)}; background: transparent;
                                 border: none; padding: 0 0 2px 0; border-radius: 11px; }}
            QPushButton#close:hover {{ color: {fg}; background: rgba(0,0,0,28); }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {surface}; border-radius: 4px;
                                           min-height: 28px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        """)
        # 摘录和按钮用黑体：字号小的时候比宋体有分量，也把「界面文字 / 正文」分开
        ui = QFont("SimHei", max(8, CFG["font_size"] - 1))
        self.head.setObjectName("head")
        self.head.setFont(ui)
        self.body.setFont(_font())
        self.entry.setFont(_font())
        self.btn_set.setFont(ui)
        self.btn_send.setFont(ui)
        self.btn_close.setFont(QFont("Segoe UI", CFG["font_size"] + 1))
        r, g, b = (int(fg[i:i + 2], 16) for i in (1, 3, 5))
        self.sep.setStyleSheet("background: rgba(%d,%d,%d,34);" % (r, g, b))
        self.update()

    # ── 窗口拖动 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    # ── 设置 ──
    def open_settings(self):
        dlg = Settings(self)
        if dlg.exec():
            self.apply_style()

    # ── 流式 ──
    def _set_busy(self, v):
        self._busy = v
        self.btn_send.setEnabled(not v)

    def _stream(self):
        self._set_busy(True)
        self.worker = Streamer(self.messages)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.done.connect(self._on_done)
        self.worker.fail.connect(self._on_fail)
        self.worker.start()

    def _on_chunk(self, s):
        if self._first:
            self.body.clear()          # 把「思考中…」擦掉
            self._first = False
        self.body.moveCursor(QTextCursor.MoveOperation.End)
        self.body.insertPlainText(s)
        sb = self.body.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_done(self):
        self.body.setHtml(_render(self.body.toPlainText()))
        # 行距：默认 100% 在中文正文里太挤，压到 176% 才像正经排版
        cur = QTextCursor(self.body.document())
        cur.select(QTextCursor.SelectionType.Document)
        bf = QTextBlockFormat()
        bf.setLineHeight(176, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cur.mergeBlockFormat(bf)
        # 流式时跟着底部走；答完回到开头，否则短答案会被顶掉第一行
        self.body.verticalScrollBar().setValue(0)
        self._set_busy(False)

    def _on_fail(self, msg):
        if self._first:
            self.body.clear()          # 别让「思考中…」留在错误信息前面
            self._first = False
        self.body.append("出错了：" + html.escape(msg))
        self._set_busy(False)

    def send_followup(self):
        q = self.entry.text().strip()
        if not q or self._busy:
            return
        self.entry.clear()
        self.messages.append({"role": "user", "content": q})
        self.body.append("<br><br>你：" + html.escape(q) + "<br><br>AI：")
        self._first = False
        self._stream()

    def show_at_right(self):
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 120, int(scr.height() * 0.15))


class Streamer(QObject):
    chunk = pyqtSignal(str)
    done = pyqtSignal()
    fail = pyqtSignal(str)

    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _emit(sig, *args):
        """窗口可能在流式过程中被销毁（连按两次热键就会这样），
        往死掉的接收者发信号会抛 RuntimeError —— 吞掉，别在后台线程里刷异常。"""
        try:
            sig.emit(*args)
        except RuntimeError:
            pass

    def _run(self):
        try:
            client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=60)
            stream = client.chat.completions.create(
                model=MODEL, messages=self.messages, temperature=0.3, stream=True)
            parts = []
            for ch in stream:
                d = ch.choices[0].delta.content
                if d:
                    parts.append(d)
                    self._emit(self.chunk, d)
            self.messages.append({"role": "assistant", "content": "".join(parts)})
            self._emit(self.done)
        except Exception as e:
            self._emit(self.fail, str(e))


# ── 设置窗口 ──────────────────────────────────────
class Settings(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        bg, fg = CFG["bg"], CFG["fg"]
        self.setStyleSheet(
            f"QDialog{{background:{bg};color:{fg};}}"
            f"QLabel{{background:transparent;color:{fg};}}"
            f"QLineEdit,QSpinBox,QComboBox,QPlainTextEdit{{"
            f"background:{_surface()};color:{fg};border:1px solid {_surface()};"
            f"border-radius:6px;padding:3px 6px;}}"
            f"QLineEdit:focus,QSpinBox:focus,QComboBox:focus,QPlainTextEdit:focus{{"
            f"border:1px solid {_accent()};}}"
            f"QPushButton{{background:{_surface()};color:{fg};border:none;"
            f"border-radius:6px;padding:5px 14px;}}"
            f"QPushButton:hover{{color:{_accent()};}}"
            f"QPushButton#ok{{background:{_accent()};color:#fff;}}")

        form = QFormLayout(self)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(9)

        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS))
        self.preset.setCurrentIndex(-1)
        self.preset.setPlaceholderText("选一个")
        self.preset.currentTextChanged.connect(self._use_preset)
        form.addRow("预设", self.preset)

        self._val = {"bg": CFG["bg"], "fg": CFG["fg"], "accent": CFG["accent"]}
        self._img_path = CFG.get("bg_image") or ""

        self.bg_btn = QPushButton()
        self.bg_btn.clicked.connect(lambda: self._pick("bg", self.bg_btn))
        form.addRow("背景色", self.bg_btn)

        self.fg_btn = QPushButton()
        self.fg_btn.clicked.connect(lambda: self._pick("fg", self.fg_btn))
        form.addRow("文字色", self.fg_btn)

        self.ac_btn = QPushButton()
        self.ac_btn.clicked.connect(lambda: self._pick("accent", self.ac_btn))
        form.addRow("强调色", self.ac_btn)

        img_row = QHBoxLayout()
        self.img_lbl = QLabel(os.path.basename(CFG.get("bg_image") or "") or "未选择")
        pick_img = QPushButton("选择")
        pick_img.clicked.connect(self._pick_image)
        clear_img = QPushButton("清除")
        clear_img.clicked.connect(self._clear_image)
        img_row.addWidget(pick_img)
        img_row.addWidget(clear_img)
        img_row.addWidget(self.img_lbl, 1)
        form.addRow("背景图", img_row)

        self.scrim = QSlider(Qt.Orientation.Horizontal)
        self.scrim.setRange(0, 100)
        self.scrim.setValue(int(CFG["scrim"]))
        self.scrim_lbl = QLabel("%d%%" % CFG["scrim"])
        self.scrim.valueChanged.connect(lambda v: self.scrim_lbl.setText("%d%%" % v))
        srow = QHBoxLayout()
        srow.addWidget(self.scrim, 1)
        srow.addWidget(self.scrim_lbl)
        form.addRow("压暗程度", srow)

        self.blur = QSlider(Qt.Orientation.Horizontal)
        self.blur.setRange(0, 60)
        self.blur.setValue(int(CFG["blur"]))
        self.blur_lbl = QLabel(str(CFG["blur"]))
        self.blur.valueChanged.connect(lambda v: self.blur_lbl.setText(str(v)))
        brow = QHBoxLayout()
        brow.addWidget(self.blur, 1)
        brow.addWidget(self.blur_lbl)
        form.addRow("背景模糊", brow)

        self.font = QComboBox()
        self.font.addItems(list(FONTS))
        self.font.setCurrentText(CFG["font"])
        form.addRow("字体", self.font)

        self.size = QSpinBox()
        self.size.setRange(7, 28)
        self.size.setValue(int(CFG["font_size"]))
        form.addRow("字号", self.size)

        self.nums = {}
        for label, key in (("扫描条数", "ctx_scan"), ("每段字数", "ctx_chars"),
                           ("用户条数", "ctx_users")):
            sp = QSpinBox()
            sp.setRange(0, 99999)
            sp.setValue(int(CFG[key]))
            self.nums[key] = sp
            form.addRow(label, sp)

        self.prompt = QPlainTextEdit(CFG["prompt"])
        self.prompt.setFixedHeight(150)
        form.addRow("提示词", self.prompt)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Save).setObjectName("ok")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

        self._paint_swatches()

    def _paint_swatches(self):
        for key, btn in (("bg", self.bg_btn), ("fg", self.fg_btn),
                         ("accent", self.ac_btn)):
            c = self._val[key]
            lum = sum(int(c[i:i + 2], 16) for i in (1, 3, 5))
            btn.setText(c)
            btn.setStyleSheet("background:%s;color:%s;border-radius:6px;padding:4px;"
                              % (c, "#fff" if lum < 380 else "#000"))

    def _use_preset(self, name):
        if name in PRESETS:
            b, f, a = PRESETS[name]
            self._val.update({"bg": b, "fg": f, "accent": a})
            self._paint_swatches()

    def _pick(self, key, btn):
        c = QColorDialog.getColor(QColor(self._val[key]), self, "选颜色")
        if c.isValid():
            self._val[key] = c.name()
            self._paint_swatches()

    def _pick_image(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选一张背景图", "", "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if p:
            self._img_path = p
            self.img_lbl.setText(os.path.basename(p))

    def _clear_image(self):
        self._img_path = ""
        self.img_lbl.setText("未选择")

    def _save(self):
        # 先校验再动配置：否则校验失败时内存里的值已经被改了一半，点取消也回不去
        p = self.prompt.toPlainText().strip()
        if not p:
            QMessageBox.warning(self, "提示词为空", "提示词不能删空。")
            return
        CFG["bg"] = self._val["bg"]
        CFG["fg"] = self._val["fg"]
        CFG["accent"] = self._val["accent"]
        CFG["font"] = self.font.currentText()
        CFG["font_size"] = self.size.value()
        CFG["scrim"] = self.scrim.value()
        CFG["blur"] = self.blur.value()
        CFG["bg_image"] = self._img_path
        for k, sp in self.nums.items():
            CFG[k] = sp.value()
        CFG["prompt"] = p
        _pix_cache.clear()
        if _save_cfg():
            self.accept()


# ── 热键线程 ──────────────────────────────────────
class Hotkey(QObject):
    ask = pyqtSignal(str)
    settings = pyqtSignal()
    quit = pyqtSignal()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        """注册热键 + 阻塞等消息

        RegisterHotKey(None,...) 把消息投递到【注册它的线程】队列，
        所以注册和 GetMessage 必须在同一线程。
        """
        ok = user32.RegisterHotKey(None, ID_ASK, MOD_CONTROL | MOD_ALT, VK_ASK)
        if not ok:
            print("！Ctrl+Alt+Space 注册失败（被其他程序占用）", flush=True)
            return
        if not user32.RegisterHotKey(None, ID_SET, MOD_CONTROL | MOD_ALT | MOD_SHIFT, VK_SET):
            print("！设置键 Ctrl+Alt+Shift+S 注册失败（不影响提问）", flush=True)
        if not user32.RegisterHotKey(None, ID_QUIT, MOD_CONTROL | MOD_ALT | MOD_SHIFT, VK_QUIT):
            print("！退出键 Ctrl+Alt+Shift+X 注册失败（不影响提问）", flush=True)
        print("热键已注册", flush=True)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message != WM_HOTKEY:
                continue
            if msg.wParam == ID_ASK:
                text = _grab_selection()          # 在后台线程抓，界面不卡
                if text:
                    self.ask.emit(text)
                else:
                    self.ask.emit("")             # 空 = 没选中，弹提示
            elif msg.wParam == ID_SET:
                self.settings.emit()
            elif msg.wParam == ID_QUIT:
                self.quit.emit()


# ── 主程序 ────────────────────────────────────────
class App:
    def __init__(self):
        self.win = None
        self.hk = Hotkey()
        self.hk.ask.connect(self.on_ask)
        self.hk.settings.connect(self.on_settings)
        self.hk.quit.connect(QApplication.instance().quit)
        self.hk.start()

    def on_ask(self, text):
        if not text:
            QMessageBox.information(None, "没选中", "先选中文字，再按 Ctrl+Alt+Space。")
            return
        if self.win is not None:
            self.win.close()
            self.win.deleteLater()
        self.win = Popup(text)
        self.win.show_at_right()
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()
        self.win.entry.setFocus()        # 打开就能直接打字追问

    def on_settings(self):
        dlg = Settings(self.win)
        if dlg.exec() and self.win is not None:
            self.win.apply_style()


def main():
    app = QApplication(sys.argv)
    QApplication.setQuitOnLastWindowClosed(False)

    if "--test" in sys.argv:
        print("key:", "已加载" if API_KEY else "缺失")
        print("剪贴板:", repr(pyperclip.paste()[:40]))
        print("配置:", CONFIG_FILE)
        return

    if not API_KEY:
        QMessageBox.critical(None, "缺少 API key",
                             "没找到 key。\n\n"
                             "把 DEEPSEEK_API_KEY 设成环境变量，或写进\n"
                             "~/.claude/settings.json 的 env.ANTHROPIC_AUTH_TOKEN。")
        return

    a = App()
    a.hk.settings.emit() if "--settings" in sys.argv else None
    print("已启动 | Ctrl+Alt+Space 问 | Ctrl+Alt+Shift+S 设置 | Ctrl+Alt+Shift+X 退出",
          flush=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
