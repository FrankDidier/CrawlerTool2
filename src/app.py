"""
主应用 - 简约可视化界面
"""
import asyncio
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
from datetime import date, datetime
import threading
import sys

import aiosqlite
import pandas as pd

try:
    from tkcalendar import DateEntry
    HAS_CALENDAR = True
except ImportError:
    HAS_CALENDAR = False

from . import auth
from . import database
from .crawlers.manager import CrawlerManager, CRAWLERS
from .export_utils import (
    export_collection_to_excel,
    export_negative_to_excel,
    export_watched_to_excel,
    backup_db,
)
from .llm import sentiment_analyze
from .notify import send_dingtalk, send_wechat

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "crawler.db"
CONFIG_PATH = ROOT / "config.yaml"

DEFAULT_CONFIG = {
    "llm": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "", "model": ""},
    "dingtalk": {"webhook_url": ""},
    "wechat": {"webhook_url": ""},
}


def load_config():
    import yaml
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or DEFAULT_CONFIG
    return DEFAULT_CONFIG


def save_config(cfg):
    import yaml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ──────────────────── 日期选择器组件 ────────────────────

class DateRangeBar(ttk.Frame):
    """日期区间选择条：起始 ~ 结束 + 筛选按钮"""

    def __init__(self, parent, on_filter, **kw):
        super().__init__(parent, **kw)
        self._on_filter = on_filter
        ttk.Label(self, text="日期:").pack(side="left", padx=(0, 4))
        if HAS_CALENDAR:
            self.start = DateEntry(self, width=10, date_pattern="yyyy-MM-dd",
                                   year=2024, month=1, day=1)
        else:
            self.start = ttk.Entry(self, width=12)
            self.start.insert(0, "2024-01-01")
        self.start.pack(side="left")
        ttk.Label(self, text=" ~ ").pack(side="left")
        if HAS_CALENDAR:
            self.end = DateEntry(self, width=10, date_pattern="yyyy-MM-dd")
        else:
            self.end = ttk.Entry(self, width=12)
            self.end.insert(0, date.today().isoformat())
        self.end.pack(side="left")
        ttk.Button(self, text="筛选", command=self._fire).pack(side="left", padx=4)
        ttk.Button(self, text="清除日期", command=self._clear).pack(side="left")

    def get_range(self):
        try:
            s = self.start.get_date() if HAS_CALENDAR else self.start.get()
            e = self.end.get_date() if HAS_CALENDAR else self.end.get()
            return str(s), str(e)
        except Exception:
            return None, None

    def _fire(self):
        self._on_filter()

    def _clear(self):
        if HAS_CALENDAR:
            self.start.set_date(date(2024, 1, 1))
            self.end.set_date(date.today())
        else:
            self.start.delete(0, "end"); self.start.insert(0, "2024-01-01")
            self.end.delete(0, "end"); self.end.insert(0, date.today().isoformat())
        self._on_filter()


# ──────────────────── 数据面板基类 ────────────────────

class DataPanel(ttk.Frame):
    """
    通用数据面板：Treeview + 全选/反选 + 日期筛选 + 导出选中
    子类需设置 columns / db_keys / table_name / _query_sql()
    """
    columns: tuple = ()
    db_keys: tuple = ()
    table_name: str = ""
    col_widths: dict = {}

    def __init__(self, parent, app: "MainApp", **kw):
        super().__init__(parent, **kw)
        self.app = app
        self._all_rows: list[dict] = []
        self._selected_iids: set[str] = set()
        self._build()

    # ── 构建 ──

    def _build(self):
        self._build_toolbar()
        self._build_treeview()

    def _build_toolbar(self):
        """子类可 override 在 toolbar 前/后加按钮"""
        self.toolbar = ttk.Frame(self)
        self.toolbar.pack(fill="x", padx=5, pady=4)

    def _build_treeview(self):
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=5)
        cols = self.columns
        self.tv = ttk.Treeview(container, columns=cols, show="headings", height=14,
                               selectmode="extended")
        for c in cols:
            w = self.col_widths.get(c, 110)
            self.tv.heading(c, text=c)
            self.tv.column(c, width=w, minwidth=40)
        scroll = ttk.Scrollbar(container, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=scroll.set)
        self.tv.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ── 选择 ──

    def _select_all(self):
        for iid in self.tv.get_children():
            self.tv.selection_add(iid)

    def _deselect_all(self):
        self.tv.selection_remove(*self.tv.get_children())

    def _get_selected_rows(self) -> list[dict]:
        sel = self.tv.selection()
        if not sel:
            return []
        indices = [self.tv.index(iid) for iid in sel]
        return [self._all_rows[i] for i in indices if i < len(self._all_rows)]

    # ── 刷新 ──

    def refresh(self, rows: list[dict] | None = None):
        if rows is not None:
            self._all_rows = rows
        for i in self.tv.get_children():
            self.tv.delete(i)
        for r in self._all_rows:
            vals = [str(r.get(k, ""))[:120] for k in self.db_keys]
            self.tv.insert("", "end", values=vals)

    # ── 导出选中 ──

    def _export_selected(self):
        rows = self._get_selected_rows()
        if not rows:
            messagebox.showwarning("提示", "请先选择要导出的行")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        df = pd.DataFrame(rows)
        keep = [k for k in self.db_keys if k in df.columns]
        df[keep].to_excel(path, index=False, engine="openpyxl")
        messagebox.showinfo("成功", f"已导出 {len(rows)} 条到 {path}")

    # ── 发送钉钉/微信 ──

    def _send_selected(self, channel: str):
        rows = self._get_selected_rows()
        if not rows:
            messagebox.showwarning("提示", "请先选择要发送的行")
            return
        cfg = load_config()
        url = cfg.get("dingtalk" if channel == "dingtalk" else "wechat", {}).get("webhook_url", "")
        if not url:
            messagebox.showwarning("提示", f"请在设置中配置 {channel} 的 webhook_url")
            return
        lines = []
        for d in rows[:30]:
            lines.append(f"[{d.get('platform','')}] {d.get('nickname','')}: "
                         f"{str(d.get('content',''))[:100]}  {d.get('link','')}")
        text = "\n".join(lines)
        title = self.table_name
        ok = send_dingtalk(url, title, text) if channel == "dingtalk" else send_wechat(url, title, text)
        messagebox.showinfo("完成" if ok else "失败", "已发送" if ok else "发送失败，请检查设置")


# ──────────────────── 采集数据面板 ────────────────────

class CollectionPanel(DataPanel):
    columns = ("平台", "ID", "昵称", "内容", "链接", "发布日期")
    db_keys = ("platform", "item_id", "nickname", "content", "link", "publish_date")
    table_name = "采集数据"
    col_widths = {"内容": 200, "链接": 160}

    def _build_toolbar(self):
        super()._build_toolbar()
        tb = self.toolbar

        ttk.Label(tb, text="搜索:").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(tb, textvariable=self.search_var, width=18)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: self._do_refresh())
        ttk.Button(tb, text="搜索", command=self._do_refresh).pack(side="left", padx=2)

        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="全选", command=self._select_all).pack(side="left", padx=2)
        ttk.Button(tb, text="反选", command=self._deselect_all).pack(side="left", padx=2)
        ttk.Button(tb, text="导出选中", command=self._export_selected).pack(side="left", padx=2)
        ttk.Button(tb, text="发送钉钉", command=lambda: self._send_selected("dingtalk")).pack(side="left", padx=2)
        ttk.Button(tb, text="发送微信", command=lambda: self._send_selected("wechat")).pack(side="left", padx=2)

        self.date_bar = DateRangeBar(self, on_filter=self._do_refresh)
        self.date_bar.pack(fill="x", padx=5, pady=(0, 2))

        ttk.Button(self, text="刷新", command=self._do_refresh).pack(anchor="w", padx=5, pady=2)

    def _do_refresh(self):
        kw = self.search_var.get().strip()
        d_start, d_end = self.date_bar.get_range()

        def do():
            async def _():
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    sql = ("SELECT id, platform, item_id, nickname, content, link, publish_date "
                           "FROM collection WHERE 1=1")
                    params: list = []
                    if kw:
                        sql += " AND (content LIKE ? OR nickname LIKE ?)"
                        params += [f"%{kw}%", f"%{kw}%"]
                    if d_start:
                        sql += " AND publish_date >= ?"
                        params.append(d_start)
                    if d_end:
                        sql += " AND publish_date <= ?"
                        params.append(d_end + " 23:59:59")
                    sql += " ORDER BY id DESC LIMIT 500"
                    cur = await db.execute(sql, params)
                    return [dict(r) for r in await cur.fetchall()]
            rows = run_async(_())
            self.after(0, lambda: self.refresh(rows))
        threading.Thread(target=do, daemon=True).start()


# ──────────────────── 负面言论面板 ────────────────────

class NegativePanel(DataPanel):
    columns = ("平台", "ID", "昵称", "内容", "链接", "情感", "备注", "发布日期")
    db_keys = ("platform", "item_id", "nickname", "content", "link", "sentiment", "remark", "publish_date")
    table_name = "负面言论"
    col_widths = {"内容": 180, "链接": 140, "备注": 140}

    def _build_toolbar(self):
        super()._build_toolbar()
        tb = self.toolbar
        ttk.Button(tb, text="语义判断", command=self.app._run_sentiment).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="全选", command=self._select_all).pack(side="left", padx=2)
        ttk.Button(tb, text="反选", command=self._deselect_all).pack(side="left", padx=2)
        ttk.Button(tb, text="导出选中", command=self._export_selected).pack(side="left", padx=2)
        ttk.Button(tb, text="发送钉钉", command=lambda: self._send_selected("dingtalk")).pack(side="left", padx=2)
        ttk.Button(tb, text="发送微信", command=lambda: self._send_selected("wechat")).pack(side="left", padx=2)

        self.date_bar = DateRangeBar(self, on_filter=self._do_refresh)
        self.date_bar.pack(fill="x", padx=5, pady=(0, 2))

        ttk.Button(self, text="刷新", command=self._do_refresh).pack(anchor="w", padx=5, pady=2)

    def _do_refresh(self):
        d_start, d_end = self.date_bar.get_range()

        def do():
            async def _():
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    sql = ("SELECT id, platform, item_id, nickname, content, link, "
                           "sentiment, remark, publish_date FROM negative WHERE 1=1")
                    params: list = []
                    if d_start:
                        sql += " AND publish_date >= ?"
                        params.append(d_start)
                    if d_end:
                        sql += " AND publish_date <= ?"
                        params.append(d_end + " 23:59:59")
                    sql += " ORDER BY id DESC LIMIT 500"
                    cur = await db.execute(sql, params)
                    return [dict(r) for r in await cur.fetchall()]
            rows = run_async(_())
            self.after(0, lambda: self.refresh(rows))
        threading.Thread(target=do, daemon=True).start()


# ──────────────────── 关注对象面板 ────────────────────

class WatchedPanel(DataPanel):
    columns = ("平台", "ID", "昵称", "内容", "链接", "关注对象", "发布日期")
    db_keys = ("platform", "item_id", "nickname", "content", "link", "watch_target_name", "publish_date")
    table_name = "关注对象"
    col_widths = {"内容": 180, "链接": 140}

    def _build_toolbar(self):
        super()._build_toolbar()
        tb = self.toolbar
        ttk.Button(tb, text="添加关注", command=self.app._add_watch).pack(side="left", padx=2)
        ttk.Button(tb, text="Excel导入", command=self.app._import_watch_excel).pack(side="left", padx=2)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(tb, text="全选", command=self._select_all).pack(side="left", padx=2)
        ttk.Button(tb, text="反选", command=self._deselect_all).pack(side="left", padx=2)
        ttk.Button(tb, text="导出选中", command=self._export_selected).pack(side="left", padx=2)
        ttk.Button(tb, text="发送钉钉", command=lambda: self._send_selected("dingtalk")).pack(side="left", padx=2)
        ttk.Button(tb, text="发送微信", command=lambda: self._send_selected("wechat")).pack(side="left", padx=2)

        self.date_bar = DateRangeBar(self, on_filter=self._do_refresh)
        self.date_bar.pack(fill="x", padx=5, pady=(0, 2))

        ttk.Button(self, text="刷新", command=self._do_refresh).pack(anchor="w", padx=5, pady=2)

    def _do_refresh(self):
        d_start, d_end = self.date_bar.get_range()

        def do():
            async def _():
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    sql = ("SELECT id, platform, item_id, nickname, content, link, "
                           "watch_target_name, publish_date FROM watched WHERE 1=1")
                    params: list = []
                    if d_start:
                        sql += " AND publish_date >= ?"
                        params.append(d_start)
                    if d_end:
                        sql += " AND publish_date <= ?"
                        params.append(d_end + " 23:59:59")
                    sql += " ORDER BY id DESC LIMIT 500"
                    cur = await db.execute(sql, params)
                    return [dict(r) for r in await cur.fetchall()]
            rows = run_async(_())
            self.after(0, lambda: self.refresh(rows))
        threading.Thread(target=do, daemon=True).start()


# ──────────────────── 登录框 ────────────────────

class LoginFrame(tk.Frame):
    def __init__(self, parent, on_success, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_success = on_success
        self._build()

    def _build(self):
        ttk.Label(self, text="爬虫小工具", font=("", 18)).pack(pady=20)
        ttk.Label(self, text="用户名").pack(anchor="w", padx=20)
        self.entry_user = ttk.Entry(self, width=30)
        self.entry_user.pack(padx=20, pady=5, fill="x")
        ttk.Label(self, text="密码").pack(anchor="w", padx=20, pady=(10, 0))
        self.entry_pwd = ttk.Entry(self, width=30, show="*")
        self.entry_pwd.pack(padx=20, pady=5, fill="x")
        ttk.Button(self, text="登录", command=self._login).pack(pady=20)

    def _login(self):
        user = self.entry_user.get().strip()
        pwd = self.entry_pwd.get()
        if not user or not pwd:
            messagebox.showerror("错误", "请输入用户名和密码")
            return
        self.entry_user.config(state="disabled")
        self.entry_pwd.config(state="disabled")

        def do():
            u = run_async(auth.login(DB_PATH, user, pwd))
            self.after(0, lambda: self._on_result(u))
        threading.Thread(target=do, daemon=True).start()

    def _on_result(self, user):
        self.entry_user.config(state="normal")
        self.entry_pwd.config(state="normal")
        if user:
            self.on_success(user)
        else:
            messagebox.showerror("错误", "用户名或密码错误")


# ──────────────────── 主界面 ────────────────────

class MainApp(tk.Frame):
    def __init__(self, parent, user: dict, **kwargs):
        super().__init__(parent, **kwargs)
        self.user = user
        self.crawler_manager = None
        self.crawler_task = None
        self._build()

    def _build(self):
        # 顶部
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=5)
        ttk.Label(top, text=f"当前用户: {self.user['username']} ({self.user['role']})").pack(side="left")
        ttk.Button(top, text="退出", command=self._logout).pack(side="right", padx=5)
        ttk.Button(top, text="设置", command=self._open_settings).pack(side="right", padx=5)

        # 平台选择
        pf = ttk.LabelFrame(self, text="采集平台")
        pf.pack(fill="x", padx=10, pady=5)
        self.platform_vars = {}
        for p in CRAWLERS:
            v = tk.BooleanVar(value=True)
            self.platform_vars[p] = v
            ttk.Checkbutton(pf, text=p, variable=v).pack(side="left", padx=10, pady=5)

        # 采集控制
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=5)
        self.btn_start = ttk.Button(ctrl, text="开始采集", command=self._start_crawl)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(ctrl, text="停止采集", command=self._stop_crawl, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        self.lbl_status = ttk.Label(ctrl, text="未运行")
        self.lbl_status.pack(side="left", padx=20)

        # 选项卡
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.panel_collection = CollectionPanel(nb, self)
        nb.add(self.panel_collection, text="采集数据")

        self.panel_negative = NegativePanel(nb, self)
        nb.add(self.panel_negative, text="负面言论")

        self.panel_watched = WatchedPanel(nb, self)
        nb.add(self.panel_watched, text="关注对象")

        f4 = ttk.Frame(nb)
        nb.add(f4, text="管理")
        ttk.Button(f4, text="全部导出：采集数据 (Excel)", command=lambda: self._export("collection")).pack(anchor="w", padx=5, pady=5)
        ttk.Button(f4, text="全部导出：负面言论 (Excel)", command=lambda: self._export("negative")).pack(anchor="w", padx=5, pady=5)
        ttk.Button(f4, text="全部导出：关注对象 (Excel)", command=lambda: self._export("watched")).pack(anchor="w", padx=5, pady=5)
        ttk.Button(f4, text="备份数据库", command=self._backup).pack(anchor="w", padx=5, pady=5)
        if self.user["role"] == "admin":
            ttk.Button(f4, text="用户管理", command=self._user_mgmt).pack(anchor="w", padx=5, pady=5)
            ttk.Button(f4, text="清空采集库（慎用）", command=self._clear_db).pack(anchor="w", padx=5, pady=5)

    # ── 设置 ──

    def _open_settings(self):
        """综合设置窗口：平台登录 + 大模型 API + 钉钉 + 微信"""
        win = tk.Toplevel(self)
        win.title("设置")
        win.geometry("500x580")
        cfg = load_config()

        # --- 平台登录 ---
        from .crawlers.browser_manager import BrowserManager
        bm = BrowserManager(DATA_DIR)

        lf0 = ttk.LabelFrame(win, text="平台登录（采集前需先登录）")
        lf0.pack(fill="x", padx=10, pady=8)
        ttk.Label(lf0, text="点击「登录」打开浏览器，手动登录后关闭浏览器窗口即可。",
                  foreground="gray").pack(anchor="w", padx=5, pady=2)

        self._login_status_labels = {}
        for platform in CRAWLERS:
            row = ttk.Frame(lf0)
            row.pack(fill="x", padx=5, pady=2)
            ttk.Label(row, text=platform, width=10).pack(side="left")
            has = bm.has_cookies(platform)
            lbl = ttk.Label(row, text="已登录" if has else "未登录",
                            foreground="green" if has else "red")
            lbl.pack(side="left", padx=10)
            self._login_status_labels[platform] = lbl

            def _make_cmd(p=platform, l=lbl, w=win):
                return lambda: self._login_platform(p, l, w)
            ttk.Button(row, text="登录", command=_make_cmd()).pack(side="right")

        # --- 大模型 ---
        lf1 = ttk.LabelFrame(win, text="大模型 API（语义判断）")
        lf1.pack(fill="x", padx=10, pady=8)
        ttk.Label(lf1, text="API 地址").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        e_url = ttk.Entry(lf1, width=50)
        e_url.insert(0, cfg.get("llm", {}).get("base_url", ""))
        e_url.grid(row=0, column=1, padx=5, pady=3)
        ttk.Label(lf1, text="API Key").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        e_key = ttk.Entry(lf1, width=50, show="*")
        e_key.insert(0, cfg.get("llm", {}).get("api_key", ""))
        e_key.grid(row=1, column=1, padx=5, pady=3)
        ttk.Label(lf1, text="模型名").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        e_model = ttk.Entry(lf1, width=50)
        e_model.insert(0, cfg.get("llm", {}).get("model", ""))
        e_model.grid(row=2, column=1, padx=5, pady=3)

        # --- 钉钉 ---
        lf2 = ttk.LabelFrame(win, text="钉钉机器人")
        lf2.pack(fill="x", padx=10, pady=8)
        ttk.Label(lf2, text="Webhook URL").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        e_ding = ttk.Entry(lf2, width=50)
        e_ding.insert(0, cfg.get("dingtalk", {}).get("webhook_url", ""))
        e_ding.grid(row=0, column=1, padx=5, pady=3)

        # --- 微信 ---
        lf3 = ttk.LabelFrame(win, text="企业微信机器人")
        lf3.pack(fill="x", padx=10, pady=8)
        ttk.Label(lf3, text="Webhook URL").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        e_wx = ttk.Entry(lf3, width=50)
        e_wx.insert(0, cfg.get("wechat", {}).get("webhook_url", ""))
        e_wx.grid(row=0, column=1, padx=5, pady=3)

        def do_save():
            cfg["llm"] = {
                "base_url": e_url.get().strip(),
                "api_key": e_key.get().strip(),
                "model": e_model.get().strip(),
            }
            cfg["dingtalk"] = {"webhook_url": e_ding.get().strip()}
            cfg["wechat"] = {"webhook_url": e_wx.get().strip()}
            save_config(cfg)
            messagebox.showinfo("成功", "设置已保存")
            win.destroy()

        ttk.Button(win, text="保存设置", command=do_save).pack(pady=10)

    def _login_platform(self, platform, status_label, parent_win):
        """Open a visible browser for the user to log in to *platform*."""
        messagebox.showinfo(
            "登录提示",
            f"即将打开 {platform} 登录页面。\n\n"
            "将自动使用系统已安装的 Chrome 或 Edge 浏览器。\n"
            "请在浏览器中完成登录，\n"
            "登录成功后关闭浏览器窗口。",
            parent=parent_win,
        )
        status_label.config(text="登录中...", foreground="orange")

        def do():
            error_msg = ""

            async def _login():
                from .crawlers.browser_manager import BrowserManager
                bm = BrowserManager(DATA_DIR)
                return await bm.login_interactive(platform)

            try:
                ok = run_async(_login())
            except Exception as exc:
                ok = False
                error_msg = str(exc)

            def update():
                if ok:
                    status_label.config(text="已登录", foreground="green")
                    messagebox.showinfo("成功", f"{platform} 登录成功！cookies 已保存。",
                                        parent=parent_win)
                else:
                    status_label.config(text="登录失败", foreground="red")
                    msg = f"{platform} 登录失败。\n\n"
                    if error_msg:
                        msg += f"错误信息:\n{error_msg}\n\n"
                    msg += "请确保系统已安装 Google Chrome 或 Microsoft Edge 浏览器。"
                    messagebox.showwarning("失败", msg, parent=parent_win)
            self.after(0, update)

        threading.Thread(target=do, daemon=True).start()

    # ── 采集 ──

    def _start_crawl(self):
        platforms = [p for p, v in self.platform_vars.items() if v.get()]
        if not platforms:
            messagebox.showwarning("提示", "请至少选择一个平台")
            return

        from .crawlers.browser_manager import BrowserManager
        bm_check = BrowserManager(DATA_DIR)
        no_cookies = [p for p in platforms if not bm_check.has_cookies(p)]
        if no_cookies:
            msg = (f"以下平台尚未登录：{', '.join(no_cookies)}\n\n"
                   "建议先在「设置」中登录各平台，否则可能无法获取数据。\n\n"
                   "是否继续？")
            if not messagebox.askyesno("提示", msg):
                return

        self.crawler_manager = CrawlerManager(DB_PATH, platforms, DATA_DIR)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text="采集中（首次启动需安装浏览器，请稍候）...")

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            mgr = self.crawler_manager

            def cb(stats):
                self.after(0, lambda: self._on_crawl_stats(stats))
            loop.run_until_complete(mgr.run_loop(cb))

        self.crawler_task = threading.Thread(target=run, daemon=True)
        self.crawler_task.start()

    def _on_crawl_stats(self, stats):
        if "error" in stats:
            self.lbl_status.config(text=f"错误: {stats['error']}")
        else:
            parts = [f"{p}:+{n}" for p, (n, _) in stats.items()]
            self.lbl_status.config(text=" | ".join(parts) if parts else "采集中...")
        self._check_watchlist()

    def _check_watchlist(self):
        async def _():
            watch_list = await database.get_watch_config(DB_PATH)
            if not watch_list:
                return
            already = await database.watched_collection_ids(DB_PATH)
            rows = await database.get_collection_batch(DB_PATH, limit=100)
            for r in rows:
                if r["id"] in already:
                    continue
                for w in watch_list:
                    if w["platform"] == r["platform"] and (
                        w["target_id"] == r.get("item_id") or w["target_name"] == r.get("nickname")
                    ):
                        await database.insert_watched(DB_PATH, {
                            **r, "collection_id": r["id"],
                            "watch_target_id": w["target_id"],
                            "watch_target_name": w.get("target_name", ""),
                        })
                        name = w.get("target_name") or w["target_id"]
                        self.after(0, lambda n=name: messagebox.showinfo(
                            "关注提醒", f"关注对象 {n} 有新发布！"))
                        self.after(0, self._play_alert)
                        break
        threading.Thread(target=lambda: run_async(_()), daemon=True).start()

    def _play_alert(self):
        try:
            import platform as _p
            if _p.system() == "Windows":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                print("\a")
        except Exception:
            print("\a")

    def _stop_crawl(self):
        if self.crawler_manager:
            self.crawler_manager.stop()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="已停止")

    # ── 语义判断 ──

    def _run_sentiment(self):
        cfg = load_config()
        api_key = cfg.get("llm", {}).get("api_key", "")
        model = cfg.get("llm", {}).get("model", "")
        if not api_key or not model:
            messagebox.showwarning("提示", "请先在「设置」中配置大模型 API Key 和模型名")
            return

        def do():
            async def _():
                batch = await database.get_collection_batch(DB_PATH, limit=20)
                if not batch:
                    self.after(0, lambda: messagebox.showinfo("提示", "暂无待分析数据"))
                    return
                texts = [r.get("content", "") for r in batch]
                results = await sentiment_analyze(
                    cfg["llm"]["base_url"], cfg["llm"]["api_key"],
                    cfg["llm"]["model"], texts,
                )
                cnt = 0
                for i, r in enumerate(batch):
                    if i < len(results) and results[i].get("sentiment") == "负面":
                        await database.insert_negative(DB_PATH, {
                            **r, "collection_id": r["id"],
                            "sentiment": "负面",
                            "remark": results[i].get("remark", ""),
                        })
                        cnt += 1
                self.after(0, lambda: (
                    messagebox.showinfo("完成", f"已分析 {len(batch)} 条，{cnt} 条负面已存入"),
                    self.panel_negative._do_refresh(),
                ))
            run_async(_())
        threading.Thread(target=do, daemon=True).start()

    # ── 关注对象 ──

    def _add_watch(self):
        win = tk.Toplevel(self)
        win.title("添加关注对象")
        ttk.Label(win, text="平台").pack(anchor="w", padx=10, pady=5)
        pvar = tk.StringVar(value="抖音")
        ttk.Combobox(win, textvariable=pvar, values=list(CRAWLERS.keys()),
                     state="readonly").pack(padx=10, pady=5, fill="x")
        ttk.Label(win, text="ID").pack(anchor="w", padx=10, pady=5)
        eid = ttk.Entry(win, width=30)
        eid.pack(padx=10, pady=5, fill="x")
        ttk.Label(win, text="昵称（选填）").pack(anchor="w", padx=10, pady=5)
        ename = ttk.Entry(win, width=30)
        ename.pack(padx=10, pady=5, fill="x")

        def save():
            p, tid, tn = pvar.get(), eid.get().strip(), ename.get().strip()
            if not tid:
                messagebox.showerror("错误", "请输入ID")
                return
            ok = run_async(database.add_watch_config(DB_PATH, p, tid, tn))
            if ok:
                messagebox.showinfo("成功", "已添加")
                win.destroy()
            else:
                messagebox.showerror("错误", "添加失败")
        ttk.Button(win, text="保存", command=save).pack(pady=10)

    def _import_watch_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx *.xls")])
        if not path:
            return
        try:
            df = pd.read_excel(path)
            cols = df.columns.tolist()
            pc = "平台" if "平台" in cols else (cols[0] if cols else "")
            ic = "ID" if "ID" in cols else ("item_id" if "item_id" in cols else (cols[1] if len(cols) > 1 else ""))
            nc = "昵称" if "昵称" in cols else ("nickname" if "nickname" in cols else "")
            if not pc or not ic:
                messagebox.showerror("错误", "Excel 需包含「平台」「ID」列")
                return

            def do():
                for _, row in df.iterrows():
                    platform = str(row.get(pc, "")).strip()
                    tid = str(row.get(ic, "")).strip()
                    tname = str(row.get(nc, "")).strip() if nc else ""
                    if tid.lower() == "nan":
                        tid = ""
                    if tname.lower() == "nan":
                        tname = ""
                    if platform and tid and platform in CRAWLERS:
                        run_async(database.add_watch_config(DB_PATH, platform, tid, tname))
                self.after(0, lambda: messagebox.showinfo("完成", "导入完成"))
            threading.Thread(target=do, daemon=True).start()
        except Exception as e:
            messagebox.showerror("错误", str(e))

    # ── 管理 ──

    def _export(self, table):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        try:
            if table == "collection":
                export_collection_to_excel(DB_PATH, Path(path))
            elif table == "negative":
                export_negative_to_excel(DB_PATH, Path(path))
            else:
                export_watched_to_excel(DB_PATH, Path(path))
            messagebox.showinfo("成功", f"已导出到 {path}")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _backup(self):
        path = backup_db(DB_PATH, DATA_DIR / "backups")
        messagebox.showinfo("成功", f"已备份到 {path}")

    def _user_mgmt(self):
        win = tk.Toplevel(self)
        win.title("用户管理")
        tv = ttk.Treeview(win, columns=("用户", "角色"), show="headings", height=10)
        tv.heading("用户", text="用户")
        tv.heading("角色", text="角色")
        users = run_async(auth.list_users(DB_PATH))
        for u in users:
            tv.insert("", "end", values=(u["username"], u["role"]))
        tv.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Button(win, text="添加用户", command=lambda: self._add_user_dialog(win)).pack(pady=5)

    def _add_user_dialog(self, parent):
        win = tk.Toplevel(parent)
        win.title("添加用户")
        ttk.Label(win, text="用户名").pack(anchor="w", padx=10, pady=5)
        eu = ttk.Entry(win, width=25)
        eu.pack(padx=10, pady=5, fill="x")
        ttk.Label(win, text="密码").pack(anchor="w", padx=10, pady=5)
        ep = ttk.Entry(win, width=25, show="*")
        ep.pack(padx=10, pady=5, fill="x")
        rv = tk.StringVar(value="user")
        ttk.Radiobutton(win, text="普通用户", variable=rv, value="user").pack(anchor="w", padx=10)
        ttk.Radiobutton(win, text="管理员", variable=rv, value="admin").pack(anchor="w", padx=10)

        def save():
            u, p = eu.get().strip(), ep.get()
            if not u or not p:
                messagebox.showerror("错误", "请输入用户名和密码")
                return
            ok, msg = run_async(auth.add_user(DB_PATH, u, p, rv.get()))
            if ok:
                messagebox.showinfo("成功", msg)
                win.destroy()
            else:
                messagebox.showerror("错误", msg)
        ttk.Button(win, text="添加", command=save).pack(pady=10)

    def _clear_db(self):
        if not messagebox.askyesno("确认", "确定清空采集库？此操作不可恢复。"):
            return
        if self.user["role"] != "admin":
            messagebox.showerror("错误", "无权限")
            return

        def do():
            async def _():
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM collection")
                    await db.commit()
                self.after(0, lambda: messagebox.showinfo("完成", "已清空"))
            run_async(_())
        threading.Thread(target=do, daemon=True).start()

    def _logout(self):
        if self.crawler_manager:
            self.crawler_manager.stop()
        root = self.winfo_toplevel()
        for w in root.winfo_children():
            w.destroy()
        LoginFrame(root, on_success=lambda user: _show_main(root, user)).pack(fill="both", expand=True)


# ──────────────────── 启动 ────────────────────

def _show_main(root, user):
    for w in root.winfo_children():
        w.destroy()
    MainApp(root, user).pack(fill="both", expand=True)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_async(database.init_db(DB_PATH))
    run_async(auth.init_auth_db(DB_PATH))

    root = tk.Tk()
    root.title("爬虫小工具")
    root.geometry("1050x650")

    LoginFrame(root, on_success=lambda user: _show_main(root, user)).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
