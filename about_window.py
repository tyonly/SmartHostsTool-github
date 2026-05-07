# -*- coding: utf-8 -*-
"""
关于窗口（Cyberpunk Glass UI, ttkbootstrap）

设计理念：
- 深色赛博朋克风格 + 玻璃拟态卡片
- 霓虹渐变色点缀，现代感十足
- 保持原有功能：打开 GitHub、展开/收起使用说明、资源路径兼容 PyInstaller
- 仍使用 Toplevel（避免第二个 Tk/mainloop）

依赖：
- ttkbootstrap（必需）
- Pillow（可选，用于更漂亮的渐变背景/头像圆形裁剪；无 Pillow 则自动降级）
"""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Optional, Sequence

import ttkbootstrap as ttk
from ttkbootstrap.constants import *

# Pillow 可选
try:
    from PIL import Image, ImageTk, ImageOps, ImageDraw, ImageFilter
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None
    ImageOps = None
    ImageDraw = None
    ImageFilter = None


# 导入自定义模块
from utils import resource_path

# 窗口尺寸配置（像素）
WINDOW_WIDTH_PX = 700
WINDOW_HEIGHT_PX = 480
EXPANDED_HEIGHT_PX = 680

# 窗口透明度
WINDOW_ALPHA = 0.95

# 头像尺寸（像素）
AVATAR_SIZE_PX = 120

# 文本控件高度（行数）
TEXT_WIDGET_HEIGHT_LINES = 10

# ========================================
# 设计规范：赛博朋克 + 玻璃拟态
# ========================================

# 配色方案
class Colors:
    # 深色背景渐变
    BG_DARK = "#0a0a0f"
    BG_CARD = "#12121a"
    BG_CARD_HOVER = "#1a1a25"
    
    # 霓虹主色（青色）
    NEON_CYAN = "#00f5ff"
    NEON_CYAN_DIM = "#00a5aa"
    
    # 霓虹辅助色（紫色）
    NEON_PURPLE = "#b967ff"
    
    # 霓虹强调色（粉色）
    NEON_PINK = "#ff6b9d"
    
    # 文字颜色
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#8888aa"
    TEXT_MUTED = "#555566"
    
    # 边框颜色
    BORDER_CYAN = "#00f5ff33"
    BORDER_PURPLE = "#b967ff33"


def hex_to_rgb(hex_color: str) -> tuple:
    """将十六进制颜色转换为RGB元组"""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def create_gradient_image(width: int, height: int, color1: str, color2: str) -> Optional[Image]:
    """创建渐变背景图片"""
    if not Image or not ImageDraw:
        return None
    try:
        img = Image.new('RGB', (width, height))
        draw = ImageDraw.Draw(img)
        r1, g1, b1 = hex_to_rgb(color1)
        r2, g2, b2 = hex_to_rgb(color2)
        for y in range(height):
            ratio = y / height
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        return img
    except Exception:
        return None


def create_neon_border(width: int, height: int, color: str, thickness: int = 2) -> Optional[Image]:
    """创建霓虹边框图片"""
    if not Image or not ImageDraw:
        return None
    try:
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        r, g, b = hex_to_rgb(color)
        # 绘制发光效果（多层叠加）
        for i in range(thickness * 3, 0, -1):
            alpha = int(80 / (i * 0.5))
            draw.rectangle([0, 0, width-1, height-1], outline=(r, g, b, alpha), width=1)
        return img
    except Exception:
        return None


def find_first_existing(paths: Sequence[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def get_git_version() -> str:
    """获取 Git 最新的 tag 版本号，如果没有则返回默认版本"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        )
        if result.returncode == 0 and result.stdout.strip():
            version = result.stdout.strip()
            # 去掉可能的前缀v
            if version.startswith('v') or version.startswith('V'):
                version = version[1:]
            return version
    except Exception:
        pass
    return "1.8"





class AboutWindow:
    """
    关于窗口：赛博朋克 + 玻璃拟态风格
    作为 Toplevel 弹窗显示（不启动第二个 mainloop）
    """

    def __init__(
        self,
        master,
        *,
        app_name: str = "智能Hosts测速工具",
        version: str = None,
        author: str = "毕加索自画像",
        github_profile_url: str = "https://github.com/KenDvD",
        github_repo_url: str = "https://github.com/KenDvD/SmartHostsTool-github",
    ) -> None:
        if version is None:
            version = get_git_version()
        self.master = master
        self.app_name = app_name
        self.version = version
        self.author = author
        self.github_profile_url = github_profile_url
        self.github_repo_url = github_repo_url

        self.window_width = WINDOW_WIDTH_PX
        self.window_height = WINDOW_HEIGHT_PX
        self.expanded_height = EXPANDED_HEIGHT_PX

        self.usage_expanded = False
        self.usage_frame = None

        self.window = ttk.Toplevel(master=master, title=f"关于 · {app_name}")
        self.window.resizable(False, False)

        # 深色背景
        self.window.configure(background=Colors.BG_DARK)

        try:
            self.window.attributes("-alpha", WINDOW_ALPHA)
        except Exception:
            pass

        # 居中
        try:
            self.window.geometry(f"{self.window_width}x{self.window_height}")
            self.window.place_window_center()
        except Exception:
            sw = self.window.winfo_screenwidth()
            sh = self.window.winfo_screenheight()
            x = int(sw / 2 - self.window_width / 2)
            y = int(sh / 2 - self.window_height / 2)
            self.window.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")

        # 模态
        try:
            self.window.transient(master)
            self.window.grab_set()
            self.window.focus_set()
        except Exception:
            pass

        self._set_icon()
        self._build_background()
        self._build_ui()

    # -------------------------
    # Icon
    # -------------------------
    def _set_icon(self) -> None:
        ico = find_first_existing([resource_path("icon.ico"), resource_path("icon.png")])
        if not ico:
            return
        try:
            if ico.lower().endswith(".ico"):
                self.window.iconbitmap(ico)
            else:
                if ImageTk and Image:
                    img = Image.open(ico)
                    photo = ImageTk.PhotoImage(img)
                    self.window.iconphoto(False, photo)
                    self.window._icon_photo = photo  # type: ignore[attr-defined]
        except Exception:
            pass

    # -------------------------
    # Background
    # -------------------------
    def _build_background(self) -> None:
        """创建赛博朋克渐变背景"""
        if not Image or not ImageTk:
            return
        try:
            # 创建深色渐变背景
            bg_img = create_gradient_image(
                self.window_width, 
                self.window_height + 200,  # 多一点高度备用
                Colors.BG_DARK, 
                "#15151f"
            )
            if bg_img:
                self._bg_photo = ImageTk.PhotoImage(bg_img)
                bg_label = ttk.Label(self.window, image=self._bg_photo)
                bg_label.place(x=0, y=0, relwidth=1, relheight=1)
                bg_label.lower()

            # 添加装饰性霓虹线条
            self._add_neon_decorations()
        except Exception:
            pass

    def _add_neon_decorations(self) -> None:
        """添加霓虹装饰元素"""
        if not Image or not ImageTk or not ImageDraw:
            return
        try:
            # 顶部霓虹线条
            line_height = 3
            line_img = Image.new('RGBA', (self.window_width, line_height * 4), (0, 0, 0, 0))
            draw = ImageDraw.Draw(line_img)
            r, g, b = hex_to_rgb(Colors.NEON_CYAN)
            for i in range(line_height * 4):
                alpha = max(0, int(60 - i * 15))
                draw.rectangle([0, i, self.window_width, i + 1], fill=(r, g, b, alpha))
            line_photo = ImageTk.PhotoImage(line_img)
            line_label = ttk.Label(self.window, image=line_photo, bg=Colors.BG_DARK)
            line_label.place(x=0, y=0)
            self.window._neon_line = line_photo  # 保持引用
        except Exception:
            pass

    # -------------------------
    # Style Setup
    # -------------------------
    def _setup_styles(self) -> None:
        """配置赛博朋克风格的ttk样式"""
        style = ttk.Style()
        
        # 深色 Frame
        style.configure("Cyber.TFrame", background=Colors.BG_CARD)
        
        # 玻璃卡片 Frame
        style.configure("Glass.TFrame", 
                       background=Colors.BG_CARD,
                       relief="flat")
        
        # 霓虹标题
        style.configure("NeonTitle.TLabel",
                       background=Colors.BG_DARK,
                       foreground=Colors.NEON_CYAN,
                       font=("Microsoft YaHei UI", 20, "bold"))
        
        # 版本标签（霓虹芯片）
        style.configure("Version.TLabelframe",
                       background=Colors.BG_CARD,
                       bordercolor=Colors.NEON_CYAN,
                       relief="flat")
        style.configure("Version.TLabelframe.Label",
                       background=Colors.BG_CARD,
                       foreground=Colors.NEON_CYAN,
                       font=("Microsoft YaHei UI", 9, "bold"))
        
        # 按钮样式
        style.configure("NeonCyan.TButton",
                       background=Colors.BG_CARD,
                       foreground=Colors.NEON_CYAN,
                       bordercolor=Colors.NEON_CYAN,
                       lightcolor=Colors.BG_CARD,
                       darkcolor=Colors.BG_CARD,
                       font=("Microsoft YaHei UI", 9))
        style.map("NeonCyan.TButton",
                 foreground=[("active", Colors.BG_DARK)],
                 background=[("active", Colors.NEON_CYAN)])
        
        # 次要按钮
        style.configure("NeonPurple.TButton",
                       background=Colors.BG_CARD,
                       foreground=Colors.NEON_PURPLE,
                       bordercolor=Colors.NEON_PURPLE,
                       font=("Microsoft YaHei UI", 9))
        style.map("NeonPurple.TButton",
                 foreground=[("active", Colors.TEXT_PRIMARY)],
                 background=[("active", Colors.NEON_PURPLE)])
        
        # 确认按钮
        style.configure("Confirm.TButton",
                       background=Colors.NEON_CYAN,
                       foreground=Colors.BG_DARK,
                       font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Confirm.TButton",
                 background=[("active", Colors.NEON_CYAN_DIM)])

    # -------------------------
    # UI
    # -------------------------
    def _build_ui(self) -> None:
        root = self.window
        self._setup_styles()

        container = ttk.Frame(root, padding=20, style="Cyber.TFrame")
        container.pack(fill=BOTH, expand=True)

        # ========== 顶部：标题区 ==========
        header = ttk.Frame(container, style="Cyber.TFrame")
        header.pack(fill=X, pady=(0, 15))

        # 霓虹标题
        title_label = ttk.Label(
            header,
            text=self.app_name,
            style="NeonTitle.TLabel",
        )
        title_label.pack(side=LEFT)

        # 版本芯片（带霓虹边框）
        version_frame = ttk.Frame(header, style="Glass.TFrame", padding=(12, 6))
        version_frame.pack(side=RIGHT)
        
        version_indicator = ttk.Label(
            version_frame,
            text="◆",
            foreground=Colors.NEON_CYAN,
            background=Colors.BG_CARD,
            font=("Microsoft YaHei UI", 8),
        )
        version_indicator.pack(side=LEFT, padx=(0, 6))
        
        version_label = ttk.Label(
            version_frame,
            text=f"v{self.version}",
            foreground=Colors.NEON_CYAN,
            background=Colors.BG_CARD,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        version_label.pack(side=LEFT)

        # ========== 主内容区 ==========
        content = ttk.Frame(container, style="Cyber.TFrame")
        content.pack(fill=BOTH, expand=True)

        # 左侧：头像卡片
        left_panel = ttk.Frame(content, style="Glass.TFrame", padding=20)
        left_panel.pack(side=LEFT, fill=Y, padx=(0, 20))
        self._render_avatar(left_panel)

        # 右侧：信息区
        right_panel = ttk.Frame(content, style="Cyber.TFrame")
        right_panel.pack(side=RIGHT, fill=BOTH, expand=True)

        # 作者信息
        author_row = ttk.Frame(right_panel, style="Cyber.TFrame")
        author_row.pack(fill=X, pady=(0, 12))

        author_icon = ttk.Label(
            author_row,
            text="◈",
            foreground=Colors.NEON_PURPLE,
            background=Colors.BG_DARK,
            font=("Microsoft YaHei UI", 12),
        )
        author_icon.pack(side=LEFT, padx=(0, 8))

        author_label = ttk.Label(
            author_row,
            text=self.author,
            foreground=Colors.TEXT_PRIMARY,
            background=Colors.BG_DARK,
            font=("Microsoft YaHei UI", 11),
        )
        author_label.pack(side=LEFT)

        # 分隔线
        self._create_neon_separator(right_panel).pack(fill=X, pady=12)

        # 项目描述
        desc_label = ttk.Label(
            right_panel,
            text="智能获取域名 IP 进行测速\n并写入 hosts 的工具",
            foreground=Colors.TEXT_SECONDARY,
            background=Colors.BG_DARK,
            font=("Microsoft YaHei UI", 10),
            justify=LEFT,
        )
        desc_label.pack(anchor=W)

        # GitHub 链接
        link_frame = ttk.Frame(right_panel, style="Cyber.TFrame", padding=(0, 12, 0, 0))
        link_frame.pack(fill=X, pady=(12, 0))

        link_icon = ttk.Label(
            link_frame,
            text="⌘",
            foreground=Colors.NEON_CYAN,
            background=Colors.BG_DARK,
            font=("Microsoft YaHei UI", 10),
        )
        link_icon.pack(side=LEFT, padx=(0, 8))

        link = ttk.Label(
            link_frame,
            text="github.com/KenDvD/SmartHostsTool",
            foreground=Colors.NEON_CYAN,
            background=Colors.BG_DARK,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
        )
        link.pack(side=LEFT)
        link.bind("<Button-1>", lambda _e: self.open_repo())

        # 警告提示（霓虹风格）
        warn_frame = ttk.Frame(right_panel, style="Glass.TFrame", padding=12)
        warn_frame.pack(fill=X, pady=(16, 0))

        warn_icon = ttk.Label(
            warn_frame,
            text="⚠",
            foreground=Colors.NEON_PINK,
            background=Colors.BG_CARD,
            font=("Microsoft YaHei UI", 12),
        )
        warn_icon.pack(side=LEFT, padx=(0, 10))

        warn_text = ttk.Label(
            warn_frame,
            text="该工具完全开源免费！\n如遇付费请立即举报",
            foreground=Colors.NEON_PINK,
            background=Colors.BG_CARD,
            font=("Microsoft YaHei UI", 9, "bold"),
            justify=LEFT,
        )
        warn_text.pack(side=LEFT)

        # ========== 底部按钮区 ==========
        btnbar = ttk.Frame(container, style="Cyber.TFrame", padding=(0, 15, 0, 0))
        btnbar.pack(fill=X)

        self.usage_btn = ttk.Button(
            btnbar,
            text="◈ 使用说明",
            command=self.toggle_usage,
            style="NeonPurple.TButton",
            width=14,
        )
        self.usage_btn.pack(side=LEFT)

        github_btn = ttk.Button(
            btnbar,
            text="⌘ GitHub",
            command=self.open_repo,
            style="NeonCyan.TButton",
            width=12,
        )
        github_btn.pack(side=LEFT, padx=(10, 0))

        confirm_btn = ttk.Button(
            btnbar,
            text="✓ 确定",
            command=self.close,
            style="Confirm.TButton",
            width=10,
        )
        confirm_btn.pack(side=RIGHT)

        # 保存引用
        self.body_frame = content
        self.usage_container = ttk.Frame(container, style="Cyber.TFrame")

        root.bind("<Escape>", lambda _e: self.close())

    def _create_neon_separator(self, parent) -> ttk.Frame:
        """创建霓虹风格分隔线"""
        sep = ttk.Frame(parent, height=2, style="Cyber.TFrame")
        
        def animate_separator():
            try:
                colors = [Colors.NEON_CYAN, Colors.NEON_PURPLE, Colors.NEON_CYAN]
                for i, c in enumerate(colors):
                    frame = ttk.Frame(sep, width=60, height=2, background=c)
                    frame.pack(side=LEFT, padx=(0 if i == 0 else 4, 0))
            except Exception:
                pass
        
        animate_separator()
        return sep

    # -------------------------
    # Avatar
    # -------------------------
    def _render_avatar(self, parent) -> None:
        """渲染赛博朋克风格的圆形头像"""
        avatar_path = resource_path("头像.jpg")

        if not (avatar_path and Image and ImageTk and ImageOps and ImageDraw):
            # 备用图标
            avatar_label = ttk.Label(
                parent,
                text="◉",
                foreground=Colors.NEON_CYAN,
                background=Colors.BG_CARD,
                font=("Microsoft YaHei UI", 72),
            )
            avatar_label.pack()
            
            hint = ttk.Label(
                parent,
                text="(未找到头像)",
                foreground=Colors.TEXT_MUTED,
                background=Colors.BG_CARD,
                font=("Microsoft YaHei UI", 8),
            )
            hint.pack(pady=(8, 0))
            return

        try:
            size = AVATAR_SIZE_PX
            
            # 打开并处理头像
            img = Image.open(avatar_path).convert("RGBA")
            # 使用 LANCZOS 重采样（新版 Pillow 使用 Resampling.LANCZOS）
            resampling = getattr(Image, 'Resampling', None) and getattr(Image.Resampling, 'LANCZOS', None) or Image.LANCZOS
            img = ImageOps.fit(img, (size, size), method=resampling)

            # 创建圆形遮罩
            mask = Image.new("L", (size, size), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, size, size), fill=255)

            # 圆形头像
            out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            out.paste(img, (0, 0), mask=mask)

            # 添加霓虹边框效果
            border_size = size + 8
            border_img = Image.new("RGBA", (border_size, border_size), (0, 0, 0, 0))
            border_draw = ImageDraw.Draw(border_img)
            r, g, b = hex_to_rgb(Colors.NEON_CYAN)
            # 发光边框
            for i in range(6, 0, -1):
                alpha = int(40 / (i * 0.5))
                border_draw.ellipse(
                    [(border_size - size) // 2 - i, (border_size - size) // 2 - i,
                     (border_size + size) // 2 + i, (border_size + size) // 2 + i],
                    outline=(r, g, b, alpha)
                )
            
            # 粘贴头像到边框上
            border_img.paste(out, ((border_size - size) // 2, (border_size - size) // 2), mask=out)
            
            photo = ImageTk.PhotoImage(border_img)
            lbl = ttk.Label(parent, image=photo, background=Colors.BG_CARD)
            lbl.pack()

            # 头像下方文字
            avatar_hint = ttk.Label(
                parent,
                text="SmartHosts",
                foreground=Colors.TEXT_SECONDARY,
                background=Colors.BG_CARD,
                font=("Microsoft YaHei UI", 9),
            )
            avatar_hint.pack(pady=(10, 0))

            self.window._avatar_photo = photo  # type: ignore[attr-defined]
            self.window._avatar_label = lbl  # type: ignore[attr-defined]
        except Exception:
            avatar_label = ttk.Label(
                parent,
                text="◉",
                foreground=Colors.NEON_CYAN,
                background=Colors.BG_CARD,
                font=("Microsoft YaHei UI", 72),
            )
            avatar_label.pack()
            hint = ttk.Label(
                parent,
                text="(头像加载失败)",
                foreground=Colors.TEXT_MUTED,
                background=Colors.BG_CARD,
                font=("Microsoft YaHei UI", 8),
            )
            hint.pack(pady=(8, 0))

    # -------------------------
    # Actions
    # -------------------------
    def open_repo(self) -> None:
        webbrowser.open(self.github_repo_url)

    def open_profile(self) -> None:
        webbrowser.open(self.github_profile_url)

    def close(self) -> None:
        try:
            self.window.grab_release()
        except Exception:
            pass
        self.window.destroy()

    def toggle_usage(self) -> None:
        """展开/收起使用说明（赛博朋克风格）"""
        if not self.usage_expanded:
            # === 展开使用说明 ===
            if self.usage_frame is None:
                self.usage_frame = ttk.Frame(self.usage_container, style="Glass.TFrame", padding=16)
                
                # 标题
                usage_title = ttk.Label(
                    self.usage_frame,
                    text="◈ 使用说明",
                    foreground=Colors.NEON_PURPLE,
                    background=Colors.BG_CARD,
                    font=("Microsoft YaHei UI", 12, "bold"),
                )
                usage_title.pack(anchor=W, pady=(0, 12))
                
                usage_content = """
1. 首先以管理员身份打开软件，点击「自定义网站预设」选择你需要测速的域名（可以自己添加想要的域名）

2. 例如 github.com：选择后点击「智能解析IP」，也可以再点击「刷新远程 Hosts」获取更多 IP
   （刷新远程 Hosts 仅 GitHub 专属，其他域名均为智能解析后测速。）

3. 点击「开始测速」——选择延迟低的 IP 写入 hosts；也可以点「一键写入最优IP」

--- 其他功能 ---

1. 刷新 DNS：清除 DNS 缓存，使 hosts 修改立即生效
2. 查看 hosts 文件：用系统默认编辑器打开系统 hosts 文件
3. 添加/删除预设：管理自定义域名列表，方便下次使用
4. 手动选择IP：按实际需求选择特定 IP 写入 hosts
5. 自动排序：测速完成后结果按延迟自动排序，方便选择最优 IP
                """.strip()

                text_frame = ttk.Frame(self.usage_frame, style="Cyber.TFrame")
                text_frame.pack(fill=BOTH, expand=True)

                # 赛博朋克风格的文本框
                text = ttk.Text(
                    text_frame,
                    wrap=WORD,
                    font=("Microsoft YaHei UI", 9),
                    height=TEXT_WIDGET_HEIGHT_LINES,
                    background=Colors.BG_DARK,
                    foreground=Colors.TEXT_SECONDARY,
                    insertbackground=Colors.NEON_CYAN,
                    relief="flat",
                    borderwidth=0,
                    padding=10,
                )
                text.insert("1.0", usage_content)
                text.configure(state="disabled")
                text.pack(side=LEFT, fill=BOTH, expand=True)

                # 滚动条
                scrollbar = ttk.Scrollbar(text_frame, width=12)
                scrollbar.pack(side=RIGHT, fill=Y, padx=(8, 0))
                scrollbar.configure(command=text.yview)
                text.configure(yscrollcommand=scrollbar.set)

            # 布局调整
            self.body_frame.pack_configure(expand=False)
            self.usage_container.pack(fill=X, expand=False, pady=(15, 0), after=self.body_frame)
            self.usage_frame.pack(fill=X, expand=False)

            # 更新状态
            self.usage_expanded = True
            self.usage_btn.configure(text="◈ 收起说明")

            # 调整窗口
            self.window.geometry(f"{self.window_width}x{self.expanded_height}")
            self.window.update_idletasks()
            self._center_window()
        else:
            # === 收起使用说明 ===
            if self.usage_frame:
                self.usage_frame.pack_forget()
            self.usage_container.pack_forget()
            self.body_frame.pack_configure(expand=True)

            # 更新状态
            self.usage_expanded = False
            self.usage_btn.configure(text="◈ 使用说明")

            # 调整窗口
            self.window.geometry(f"{self.window_width}x{self.window_height}")
            self.window.update_idletasks()
            self._center_window()

    def _center_window(self) -> None:
        """窗口居中"""
        try:
            self.window.place_window_center()
        except Exception:
            sw = self.window.winfo_screenwidth()
            sh = self.window.winfo_screenheight()
            w, h = self.window.winfo_width(), self.window.winfo_height()
            x = int(sw / 2 - w / 2)
            y = int(sh / 2 - h / 2)
            self.window.geometry(f"{w}x{h}+{x}+{y}")


if __name__ == "__main__":
    app = ttk.Window(themename="darkly")
    app.title("AboutWindow 测试")
    app.geometry("700x480")
    AboutWindow(app)
    app.mainloop()