# -*- coding: utf-8 -*-
"""
main.py

程序入口：
- 支持 writer mode：用于“自动提权后仅写入 hosts 内容并退出”
- 正常模式：启动 GUI

说明：
- writer mode 由 hosts_file.HostsFileManager.write_hosts_atomic 触发：
  会把写入内容保存到临时文件，然后以管理员权限重启并传入：
    --write-content=<tempfile> --encoding=<encoding>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from config import APP_NAME, APP_THEME, LOG_CONFIG, TRAY_CONFIG
from hosts_file import HostsFileManager
from utils import check_and_elevate, get_logger, resource_path, setup_logger


def _run_writer_mode(write_content_path: str, encoding: str) -> None:
    """提权后的写入模式：写入 hosts -> 刷新 DNS -> 退出。"""
    logger = get_logger()
    mgr = HostsFileManager()

    if not (write_content_path and os.path.exists(write_content_path)):
        logger.error("writer mode: 临时内容文件不存在，退出。")
        sys.exit(0)

    success = False
    try:
        with open(write_content_path, "r", encoding=encoding) as f:
            content = f.read()

        # 这里关闭"再次提权"，避免循环
        mgr.write_hosts_atomic(content, encoding=encoding, allow_elevate=False)
        success = True
        logger.info("Hosts文件写入成功（writer mode）")
    except Exception as e:
        logger.exception(f"writer mode: 写入 hosts 失败: {e}")
    finally:
        try:
            os.remove(write_content_path)
        except Exception as e:
            logger.warning(f"删除临时文件失败: {e}")

    if success:
        try:
            mgr.flush_dns_cache()
            logger.info("DNS缓存已刷新（writer mode）")
        except Exception as e:
            logger.error(f"writer mode: 刷新DNS失败: {e}")

    sys.exit(0)


def main() -> None:
    # 初始化日志系统（最早初始化，确保所有模块都能使用日志）
    log_level_str = LOG_CONFIG.get("level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    
    logger = setup_logger(
        APP_NAME,
        log_level=log_level,
        max_bytes=LOG_CONFIG.get("max_bytes", 10 * 1024 * 1024),
        backup_count=LOG_CONFIG.get("backup_count", 5),
        console_output=LOG_CONFIG.get("console_output", True),
    )
    
    logger.info("=" * 60)
    logger.info(f"{APP_NAME} 启动")
    logger.info(f"Python 版本: {sys.version}")
    logger.info(f"平台: {sys.platform}")
    logger.info("=" * 60)

    parser = argparse.ArgumentParser()
    parser.add_argument("--write-content", type=str, help="临时文件路径，包含要写入的 hosts 内容")
    parser.add_argument("--encoding", type=str, default="utf-8", help="写入内容的编码")
    args = parser.parse_args()

    # writer mode：仅执行写入动作并退出
    if args.write_content:
        logger.info(f"进入 writer mode，临时文件: {args.write_content}")
        _run_writer_mode(args.write_content, args.encoding)

    # 正常 GUI 启动：先请求管理员权限
    logger.info("检查管理员权限...")
    check_and_elevate()
    logger.info("管理员权限检查通过")

    # Windows 任务栏图标稳定性：设置 AppUserModelID（避免被 python.exe 默认图标/分组覆盖）
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
        except Exception:
            pass

    import ttkbootstrap as ttk  # 延迟导入，避免 writer mode 拉起 GUI 依赖
    from main_window import HostsOptimizer

    logger.info("初始化 GUI 界面...")
    app = ttk.Window(themename=APP_THEME)
    app.title("智能 Hosts 测速工具")
    app.geometry("1080x680")
    app.minsize(980, 620)

    # 启动体验优化：避免先显示一个“空的大窗口 + 正在加载”再卡住几秒。
    # 做法：先隐藏主窗口，仅显示一个小的加载提示窗；初始化完成后再显示主窗口。
    app.withdraw()
    splash = ttk.Toplevel(app)
    splash.title(APP_NAME)
    try:
        splash.overrideredirect(True)  # 无边框小窗
    except Exception:
        pass
    splash.attributes("-topmost", True)
    splash.geometry("360x120")
    splash.resizable(False, False)
    splash_label = ttk.Label(splash, text="正在加载…", font=("Segoe UI", 14))
    splash_label.pack(expand=True, fill="both", padx=18, pady=18)
    try:
        splash.update_idletasks()
        x = (splash.winfo_screenwidth() // 2) - (360 // 2)
        y = (splash.winfo_screenheight() // 2) - (120 // 2)
        splash.geometry(f"360x120+{x}+{y}")
    except Exception:
        pass
    splash.update()

    ico = resource_path("icon.ico")
    if os.path.exists(ico):
        # 先用 iconbitmap 尽早覆盖窗口/任务栏图标（避免显示 Python 默认图标）
        try:
            app.iconbitmap(ico)
        except Exception:
            pass
        try:
            splash.iconbitmap(ico)
        except Exception:
            pass
        try:
            from PIL import Image, ImageTk
            # 使用 PIL 加载并转换图标格式
            img = Image.open(ico)
            # 确保图像为 32 位 RGBA 格式
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            # 调整为合适大小
            img = img.resize((256, 256), Image.Resampling.LANCZOS)
            icon = ImageTk.PhotoImage(img)
            # iconphoto(True) 设置为窗口和任务栏的默认图标
            app.iconphoto(True, icon)
            # 必须保留引用，避免被 GC 回收后回退到默认（Python 图标）
            try:
                app._iconphoto_ref = icon  # type: ignore[attr-defined]
            except Exception:
                pass
            logger.debug(f"设置窗口图标成功: {ico}")
        except Exception as e:
            logger.warning(f"设置窗口图标失败: {e}")
            try:
                # 备用方案：使用 iconbitmap
                app.iconbitmap(ico)
            except Exception:
                pass

    # splash 结束后立即显示主窗口（占位层），重活放到 mainloop 里分段初始化
    loading_frame = ttk.Frame(app, padding=18)
    loading_frame.pack(fill="both", expand=True)
    ttk.Label(loading_frame, text="正在初始化…", font=("Segoe UI", 14)).pack(pady=(140, 10))
    try:
        pb = ttk.Progressbar(loading_frame, mode="indeterminate")
        pb.pack(fill="x", padx=120)
        pb.start(10)
    except Exception:
        pb = None

    try:
        # 居中并确保不会跑到屏幕外
        app.update_idletasks()
        try:
            app.place_window_center()
        except Exception:
            sw = app.winfo_screenwidth()
            sh = app.winfo_screenheight()
            w = max(1, app.winfo_width())
            h = max(1, app.winfo_height())
            x = max(0, int(sw / 2 - w / 2))
            y = max(0, int(sh / 2 - h / 2))
            app.geometry(f"{w}x{h}+{x}+{y}")
        app.deiconify()
    except Exception:
        pass

    try:
        splash.destroy()
    except Exception:
        pass

    state: dict = {"hosts_optimizer": None, "tray_icon": None}

    def _finish_bootstrap() -> None:
        """在主循环中初始化主界面与托盘，避免 splash 后黑屏等待。"""
        try:
            # 关键：不要在初始化主界面前销毁占位层，否则会出现“空白卡住”。
            # 占位层保持显示，等 HostsOptimizer 初始化完成后再销毁。
            try:
                app.update_idletasks()
            except Exception:
                pass

            hosts_optimizer = HostsOptimizer(app)
            state["hosts_optimizer"] = hosts_optimizer

            # 主界面初始化完成后再移除占位层
            try:
                if pb is not None:
                    pb.stop()
            except Exception:
                pass
            try:
                loading_frame.destroy()
            except Exception:
                pass

            # 初始化系统托盘（如果可用）
            tray_icon = None
            if TRAY_CONFIG.get("minimize_to_tray", True):
                try:
                    from tray_icon import SystemTrayIcon, check_tray_dependencies

                    available, missing = check_tray_dependencies()
                    if available:
                        logger.info("初始化系统托盘...")
                        tray_icon = SystemTrayIcon(
                            app_name=APP_NAME,
                            on_show_window=hosts_optimizer.show_window,
                            on_hide_window=hosts_optimizer.hide_window,
                            on_quick_test=lambda: app.after(0, hosts_optimizer.start_test),
                            on_flush_dns=lambda: app.after(0, lambda: hosts_optimizer.flush_dns(silent=True)),
                            on_exit=lambda: app.after(0, hosts_optimizer.force_exit),
                        )

                        if tray_icon.start():
                            hosts_optimizer.set_tray_icon(tray_icon)
                            logger.info("系统托盘初始化成功")
                        else:
                            logger.warning("系统托盘启动失败")
                            tray_icon = None
                    else:
                        logger.info(f"系统托盘不可用，缺少依赖: {', '.join(missing)}")
                        logger.info("如需使用托盘功能，请运行: pip install pystray Pillow")
                except ImportError as e:
                    logger.warning(f"无法导入托盘模块: {e}")
                except Exception as e:
                    logger.warning(f"初始化托盘时出错: {e}")
            state["tray_icon"] = tray_icon

            # 如果配置了启动时最小化（在 UI 初始化完成后再处理）
            if TRAY_CONFIG.get("start_minimized", False) and tray_icon and tray_icon.is_running:
                logger.info("启动时最小化到托盘")
                app.after(100, hosts_optimizer.hide_window)
        except Exception as e:
            logger.exception(f"初始化主界面失败: {e}")

    logger.info("GUI 主循环启动（延迟初始化主界面）")
    app.after(10, _finish_bootstrap)
    app.mainloop()
    
    # 注意：托盘清理已在 force_exit() 中处理（窗口销毁时）
    logger.info("程序退出")


if __name__ == "__main__":
    main()
