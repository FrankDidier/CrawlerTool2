#!/usr/bin/env python3
"""
GUI 测试 - 验证新面板结构、工具栏、设置窗口等
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "crawler.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)

results = []


def log(name, ok, detail=""):
    tag = "✓" if ok else "✗"
    results.append((name, ok))
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))


def test_gui():
    from src import database, auth
    from src.app import (LoginFrame, MainApp, run_async, _show_main,
                         CollectionPanel, NegativePanel, WatchedPanel, DateRangeBar)

    run_async(database.init_db(DB_PATH))
    run_async(auth.init_auth_db(DB_PATH))

    import tkinter as tk

    root = tk.Tk()
    root.title("GUI 测试")
    root.geometry("1050x650")

    print("\n" + "=" * 50)
    print("GUI 功能测试")
    print("=" * 50)

    # ── 1. Login ──
    def phase_login():
        print("\n[1] 登录界面")
        children = root.winfo_children()
        frame = next((c for c in children if isinstance(c, LoginFrame)), None)
        log("LoginFrame 存在", frame is not None)
        if frame:
            log("用户名输入框", hasattr(frame, "entry_user"))
            log("密码输入框", hasattr(frame, "entry_pwd"))
        root.after(200, phase_main)

    # ── 2. Main screen ──
    def phase_main():
        print("\n[2] 主界面 + 面板结构")
        _show_main(root, {"id": 1, "username": "admin", "role": "admin"})
        root.update_idletasks()
        app = next((c for c in root.winfo_children() if isinstance(c, MainApp)), None)
        log("MainApp 加载", app is not None)
        if not app:
            root.after(100, root.destroy)
            return

        log("采集面板", isinstance(app.panel_collection, CollectionPanel))
        log("负面面板", isinstance(app.panel_negative, NegativePanel))
        log("关注面板", isinstance(app.panel_watched, WatchedPanel))

        root.after(200, lambda: phase_panels(app))

    # ── 3. Panel features ──
    def phase_panels(app):
        print("\n[3] 面板功能")

        # 采集面板
        cp = app.panel_collection
        log("采集面板: 搜索框", hasattr(cp, "search_var"))
        log("采集面板: 日期选择器", hasattr(cp, "date_bar") and isinstance(cp.date_bar, DateRangeBar))
        log("采集面板: Treeview", hasattr(cp, "tv"))
        cp_cols = list(cp.tv["columns"])
        log("采集面板: 含链接列", "链接" in cp_cols, f"列: {cp_cols}")

        # 负面面板
        np_ = app.panel_negative
        np_cols = list(np_.tv["columns"])
        log("负面面板: 含链接列", "链接" in np_cols, f"列: {np_cols}")
        log("负面面板: 含情感列", "情感" in np_cols)
        log("负面面板: 含备注列", "备注" in np_cols)
        log("负面面板: 日期选择器", hasattr(np_, "date_bar"))

        # 关注面板
        wp = app.panel_watched
        wp_cols = list(wp.tv["columns"])
        log("关注面板: 含链接列", "链接" in wp_cols, f"列: {wp_cols}")
        log("关注面板: 含关注对象列", "关注对象" in wp_cols)
        log("关注面板: 日期选择器", hasattr(wp, "date_bar"))

        root.after(200, lambda: phase_crawl(app))

    # ── 4. Crawl control ──
    def phase_crawl(app):
        print("\n[4] 采集控制")
        log("开始按钮可用", str(app.btn_start["state"]) == "normal")
        log("停止按钮禁用", str(app.btn_stop["state"]) == "disabled")
        log("状态「未运行」", app.lbl_status.cget("text") == "未运行")

        app._start_crawl()
        root.update_idletasks()
        log("采集中: 开始禁用", str(app.btn_start["state"]) == "disabled")
        log("采集中: 停止可用", str(app.btn_stop["state"]) != "disabled")

        app._stop_crawl()
        root.update_idletasks()
        log("停止后: 已停止", app.lbl_status.cget("text") == "已停止")

        root.after(200, lambda: phase_settings(app))

    # ── 5. Settings dialog ──
    def phase_settings(app):
        print("\n[5] 设置窗口")
        app._open_settings()
        root.update_idletasks()

        def find_toplevels(widget):
            out = []
            for w in widget.winfo_children():
                if isinstance(w, tk.Toplevel):
                    out.append(w)
                out.extend(find_toplevels(w))
            return out

        toplevels = [w for w in find_toplevels(root) if w.title() == "设置"]
        log("设置窗口打开", len(toplevels) >= 1)

        if toplevels:
            win = toplevels[0]
            lfs = [w for w in win.winfo_children() if isinstance(w, ttk.LabelFrame)]
            labels = [lf.cget("text") for lf in lfs]
            log("设置: 大模型区域", any("大模型" in l for l in labels), f"{labels}")
            log("设置: 钉钉区域", any("钉钉" in l for l in labels))
            log("设置: 微信区域", any("微信" in l for l in labels))
            win.destroy()

        root.after(200, lambda: phase_logout(app))

    # ── 6. Logout ──
    def phase_logout(app):
        print("\n[6] 退出 + 重登录")
        try:
            app._logout()
            root.update_idletasks()
            log("退出无崩溃", True)
        except Exception as e:
            log("退出无崩溃", False, str(e))
            root.after(100, root.destroy)
            return

        login_found = any(isinstance(c, LoginFrame) for c in root.winfo_children())
        log("返回登录界面", login_found)

        _show_main(root, {"id": 1, "username": "admin", "role": "admin"})
        root.update_idletasks()
        main_found = any(isinstance(c, MainApp) for c in root.winfo_children())
        log("重新登录成功", main_found)

        root.after(300, root.destroy)

    LoginFrame(root, on_success=lambda u: _show_main(root, u)).pack(fill="both", expand=True)
    root.after(300, phase_login)
    root.after(20000, root.destroy)

    root.mainloop()

    p = sum(1 for _, ok in results if ok)
    f = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 50}")
    print(f"结果: {p}/{p + f} 通过, {f} 失败")
    if f:
        for name, ok in results:
            if not ok:
                print(f"  ✗ {name}")
        sys.exit(1)
    else:
        print("GUI 测试全部通过 ✓")
    print("=" * 50)


if __name__ == "__main__":
    test_gui()
