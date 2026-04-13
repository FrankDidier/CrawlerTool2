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

def _get_root() -> Path:
    """Stable root directory that persists between runs.

    When packaged with PyInstaller, __file__ points to a temporary
    extraction directory that's deleted on exit.  Use the directory
    containing the EXE instead so data, config, and cookies survive.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _get_root()
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "crawler.db"
CONFIG_PATH = ROOT / "config.yaml"

DEFAULT_CONFIG = {
    "llm": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "", "model": ""},
    "dingtalk": {"webhook_url": ""},
    "wechat": {"webhook_url": ""},
    "crawler": {"target_city": ""},
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
        self.tv.bind("<Double-1>", self._on_double_click)

    def _on_double_click(self, event):
        """Double-click a row to open the link column in a browser."""
        item = self.tv.identify_row(event.y)
        if not item:
            return
        idx = self.tv.index(item)
        if idx >= len(self._all_rows):
            return
        row = self._all_rows[idx]
        link = row.get("link", "")
        if link:
            import webbrowser
            webbrowser.open(link)

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
        title = "出现相关负面舆情，请及时关注处理"
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

        ttk.Label(tb, text="平台:").pack(side="left")
        self.platform_filter = tk.StringVar(value="全部")
        pf_combo = ttk.Combobox(
            tb, textvariable=self.platform_filter, width=8,
            values=["全部"] + list(CRAWLERS.keys()), state="readonly",
        )
        pf_combo.pack(side="left", padx=4)
        pf_combo.bind("<<ComboboxSelected>>", lambda _: self._do_refresh())

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
        pf = self.platform_filter.get()

        def do():
            async def _():
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    sql = ("SELECT id, platform, item_id, nickname, content, link, publish_date "
                           "FROM collection WHERE 1=1")
                    params: list = []
                    if pf and pf != "全部":
                        sql += " AND platform = ?"
                        params.append(pf)
                    if kw:
                        sql += " AND (content LIKE ? OR nickname LIKE ?)"
                        params += [f"%{kw}%", f"%{kw}%"]
                    if d_start:
                        sql += (" AND (publish_date >= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
                        params.append(d_start)
                    if d_end:
                        sql += (" AND (publish_date <= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
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
        ttk.Button(tb, text="按条件分析", command=self.app._run_sentiment_filtered).pack(side="left", padx=2)
        ttk.Button(tb, text="分析全部", command=self.app._run_sentiment_all).pack(side="left", padx=2)
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
                        sql += (" AND (publish_date >= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
                        params.append(d_start)
                    if d_end:
                        sql += (" AND (publish_date <= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
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
        ttk.Button(tb, text="查看关注列表", command=self.app._view_watch_list).pack(side="left", padx=2)
        ttk.Button(tb, text="Excel导入", command=self.app._import_watch_excel).pack(side="left", padx=2)
        ttk.Button(tb, text="下载模板", command=self.app._download_watch_template).pack(side="left", padx=2)
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
                        sql += (" AND (publish_date >= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
                        params.append(d_start)
                    if d_end:
                        sql += (" AND (publish_date <= ? OR publish_date = ''"
                                " OR publish_date IS NULL)")
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
        ttk.Button(f4, text="导出日志（发送给技术支持排查问题）",
                   command=self._export_logs).pack(anchor="w", padx=5, pady=5)
        if self.user["role"] == "admin":
            ttk.Button(f4, text="用户管理", command=self._user_mgmt).pack(anchor="w", padx=5, pady=5)
            ttk.Button(f4, text="清空采集库（慎用）", command=self._clear_db).pack(anchor="w", padx=5, pady=5)

    # ── 设置 ──

    def _open_settings(self):
        """综合设置窗口：平台登录 + 大模型 API + 钉钉 + 微信"""
        win = tk.Toplevel(self)
        win.title("设置")
        win.geometry("520x720")
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

        # --- 采集设置 ---
        lf_crawl = ttk.LabelFrame(win, text="采集设置")
        lf_crawl.pack(fill="x", padx=10, pady=8)
        ttk.Label(lf_crawl, text="目标城市").grid(
            row=0, column=0, sticky="w", padx=5, pady=3)
        e_city = ttk.Entry(lf_crawl, width=50)
        e_city.insert(0, cfg.get("crawler", {}).get("target_city", ""))
        e_city.grid(row=0, column=1, padx=5, pady=3)
        ttk.Label(lf_crawl,
                  text="填写后将自动尝试多种策略搜索该城市同城内容"
                       "（隐身/持久化/扩展/用户Chrome）",
                  foreground="gray").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 3))

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

        def test_llm_api():
            url = e_url.get().strip()
            key = e_key.get().strip()
            mdl = e_model.get().strip()
            if not key or not mdl:
                messagebox.showwarning("提示", "请先填写 API Key 和模型名",
                                       parent=win)
                return

            test_lbl.config(text="测试中...", foreground="orange")

            def do():
                async def _():
                    try:
                        results = await sentiment_analyze(
                            url, key, mdl, ["这是一条测试文本，今天天气不错"])
                        r = results[0] if results else {}
                        s = r.get("sentiment", "未知")
                        remark = r.get("remark", "")
                        if "未配置" in remark or "解析失败" in remark:
                            raise Exception(remark)
                        self.after(0, lambda: (
                            test_lbl.config(text="测试成功！API 可正常调用",
                                            foreground="green"),
                            messagebox.showinfo(
                                "测试成功",
                                f"API 调用正常！\n\n"
                                f"测试结果: 情感={s}\n备注: {remark}",
                                parent=win,
                            ),
                        ))
                    except Exception as e:
                        err = str(e)
                        self.after(0, lambda: (
                            test_lbl.config(text="测试失败", foreground="red"),
                            messagebox.showerror(
                                "测试失败",
                                f"API 调用失败。\n\n"
                                f"错误信息:\n{err}\n\n"
                                "请检查:\n"
                                "1. API 地址是否正确\n"
                                "2. API Key 是否有效（是否欠费）\n"
                                "3. 模型名是否正确",
                                parent=win,
                            ),
                        ))
                run_async(_())
            threading.Thread(target=do, daemon=True).start()

        test_row = ttk.Frame(lf1)
        test_row.grid(row=3, column=0, columnspan=2, pady=5)
        ttk.Button(test_row, text="测试 API", command=test_llm_api).pack(side="left", padx=5)
        test_lbl = ttk.Label(test_row, text="", foreground="gray")
        test_lbl.pack(side="left", padx=5)

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
            cfg["crawler"] = {"target_city": e_city.get().strip()}
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

        cfg = load_config()
        target_city = cfg.get("crawler", {}).get("target_city", "")

        def _status_cb(msg):
            self.after(0, lambda m=msg: self._on_strategy_status(m))

        self.crawler_manager = CrawlerManager(
            DB_PATH, platforms, DATA_DIR,
            target_city=target_city,
            status_callback=_status_cb)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text="采集启动中...")

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
        self.panel_collection._do_refresh()
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
                    nick = r.get("nickname", "")
                    iid = r.get("item_id", "")
                    wid = w["target_id"]
                    wname = w.get("target_name", "")
                    if w["platform"] == r["platform"] and (
                        (wid and wid in (iid, nick))
                        or (wname and wname == nick)
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

    def _on_strategy_status(self, msg: str):
        """Handle strategy status messages from crawlers."""
        self.lbl_status.config(text=msg)
        trigger_words = ("正在尝试", "成功", "出错", "兜底", "验证码",
                         "未获取", "下一方案")
        if any(w in msg for w in trigger_words):
            self._show_strategy_popup(msg)

    def _show_strategy_popup(self, msg: str):
        """Show a brief auto-closing notification popup."""
        try:
            if (hasattr(self, '_strategy_popup')
                    and self._strategy_popup.winfo_exists()):
                self._strategy_popup.destroy()
        except Exception:
            pass

        win = tk.Toplevel(self)
        win.title("采集策略通知")
        win.geometry("460x90")
        win.attributes('-topmost', True)
        win.resizable(False, False)

        root_w = self.winfo_toplevel()
        x = root_w.winfo_x() + root_w.winfo_width() // 2 - 230
        y = root_w.winfo_y() + 60
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

        ttk.Label(win, text=msg, wraplength=440,
                  justify="center").pack(padx=10, pady=25,
                                         fill="both", expand=True)
        self._strategy_popup = win
        win.after(5000, lambda: win.destroy()
                  if win.winfo_exists() else None)

    def _stop_crawl(self):
        if self.crawler_manager:
            self.crawler_manager.stop()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="已停止")

    # ── 语义判断 ──

    def _check_llm_config(self):
        cfg = load_config()
        api_key = cfg.get("llm", {}).get("api_key", "")
        model = cfg.get("llm", {}).get("model", "")
        if not api_key or not model:
            messagebox.showwarning("提示", "请先在「设置」中配置大模型 API Key 和模型名")
            return None
        return cfg

    def _run_sentiment_filtered(self):
        """Open dialog for user to select date range and platforms, then batch analyze."""
        cfg = self._check_llm_config()
        if not cfg:
            return

        win = tk.Toplevel(self)
        win.title("按条件批量分析")
        win.geometry("400x320")

        ttk.Label(win, text="选择平台（可多选）:").pack(anchor="w", padx=10, pady=(10, 2))
        pf_frame = ttk.Frame(win)
        pf_frame.pack(fill="x", padx=10)
        pf_vars = {}
        for p in CRAWLERS:
            v = tk.BooleanVar(value=True)
            pf_vars[p] = v
            ttk.Checkbutton(pf_frame, text=p, variable=v).pack(side="left", padx=5)

        ttk.Label(win, text="日期范围:").pack(anchor="w", padx=10, pady=(10, 2))
        date_frame = ttk.Frame(win)
        date_frame.pack(fill="x", padx=10)
        if HAS_CALENDAR:
            d_start = DateEntry(date_frame, width=12, date_pattern="yyyy-MM-dd",
                                year=2024, month=1, day=1)
        else:
            d_start = ttk.Entry(date_frame, width=12)
            d_start.insert(0, "2024-01-01")
        d_start.pack(side="left")
        ttk.Label(date_frame, text=" ~ ").pack(side="left")
        if HAS_CALENDAR:
            d_end = DateEntry(date_frame, width=12, date_pattern="yyyy-MM-dd")
        else:
            d_end = ttk.Entry(date_frame, width=12)
            d_end.insert(0, date.today().isoformat())
        d_end.pack(side="left")

        info_lbl = ttk.Label(win, text="", foreground="gray")
        info_lbl.pack(anchor="w", padx=10, pady=5)

        progress_var = tk.StringVar(value="")
        progress_lbl = ttk.Label(win, textvariable=progress_var, foreground="blue")
        progress_lbl.pack(anchor="w", padx=10, pady=2)

        def count_items():
            platforms = [p for p, v in pf_vars.items() if v.get()]
            ds = str(d_start.get_date() if HAS_CALENDAR else d_start.get())
            de = str(d_end.get_date() if HAS_CALENDAR else d_end.get())

            def do():
                items = run_async(database.get_unanalyzed_collection(
                    DB_PATH, platforms=platforms, date_start=ds, date_end=de))
                self.after(0, lambda: info_lbl.config(
                    text=f"符合条件的未分析数据: {len(items)} 条"))
            threading.Thread(target=do, daemon=True).start()

        ttk.Button(win, text="查询数量", command=count_items).pack(pady=5)

        def start_analysis():
            platforms = [p for p, v in pf_vars.items() if v.get()]
            ds = str(d_start.get_date() if HAS_CALENDAR else d_start.get())
            de = str(d_end.get_date() if HAS_CALENDAR else d_end.get())
            self._do_batch_sentiment(cfg, platforms, ds, de, 0, progress_var, win)

        ttk.Button(win, text="开始分析", command=start_analysis).pack(pady=10)

    def _run_sentiment_all(self):
        """Analyze all unanalyzed items in the database."""
        cfg = self._check_llm_config()
        if not cfg:
            return
        cnt = run_async(database.count_unanalyzed(DB_PATH))
        if cnt == 0:
            messagebox.showinfo("提示", "所有数据已分析完毕，无新增待分析项。")
            return
        if not messagebox.askyesno(
            "确认",
            f"采集库中共有 {cnt} 条未分析数据。\n\n"
            "将提交全部数据进行批量语义分析。\n"
            "已分析过的数据不会重复分析。\n\n"
            "是否开始？",
        ):
            return
        win = tk.Toplevel(self)
        win.title("批量分析全部数据")
        win.geometry("450x100")
        progress_var = tk.StringVar(value="准备中...")
        ttk.Label(win, textvariable=progress_var).pack(padx=10, pady=20)
        self._do_batch_sentiment(cfg, None, None, None, 0, progress_var, win)

    def _do_batch_sentiment(self, cfg, platforms, date_start, date_end,
                            limit, progress_var, win):
        BATCH = 50

        def do():
            import time as _time

            async def _():
                items = await database.get_unanalyzed_collection(
                    DB_PATH, platforms=platforms,
                    date_start=date_start, date_end=date_end,
                    limit=limit,
                )
                total = len(items)
                if total == 0:
                    self.after(0, lambda: (
                        progress_var.set("无待分析数据（已全部分析或无匹配项）"),
                        messagebox.showinfo("完成", "无新增待分析数据。", parent=win),
                    ))
                    return

                t0 = _time.time()
                analyzed = 0
                negative_cnt = 0
                for start in range(0, total, BATCH):
                    batch = items[start:start + BATCH]
                    elapsed = _time.time() - t0
                    speed = analyzed / elapsed if elapsed > 1 else 0
                    eta = (total - analyzed) / speed if speed > 0 else 0
                    self.after(0, lambda s=analyzed, t=total, e=int(eta):
                               progress_var.set(
                                   f"分析中 {s}/{t}（预计剩余 {e} 秒）"))
                    texts = [r.get("content", "") for r in batch]
                    try:
                        results = await sentiment_analyze(
                            cfg["llm"]["base_url"], cfg["llm"]["api_key"],
                            cfg["llm"]["model"], texts,
                        )
                    except Exception as e:
                        self.after(0, lambda e=e: (
                            progress_var.set(f"API 调用出错: {e}"),
                            messagebox.showerror("错误",
                                                 f"大模型 API 调用失败:\n{e}", parent=win),
                        ))
                        return
                    for i, r in enumerate(batch):
                        analyzed += 1
                        if i < len(results) and results[i].get("sentiment") == "负面":
                            await database.insert_negative(DB_PATH, {
                                **r, "collection_id": r["id"],
                                "sentiment": "负面",
                                "remark": results[i].get("remark", ""),
                            })
                            negative_cnt += 1
                    self.after(0, lambda a=analyzed, t=total, n=negative_cnt:
                               progress_var.set(
                                   f"已分析 {a}/{t}，负面 {n} 条"))

                elapsed_total = int(_time.time() - t0)
                self.after(0, lambda: (
                    progress_var.set(
                        f"完成！共分析 {analyzed} 条，发现 {negative_cnt} 条负面"
                        f"（用时 {elapsed_total} 秒）"),
                    messagebox.showinfo(
                        "完成",
                        f"共分析 {analyzed} 条数据\n"
                        f"发现 {negative_cnt} 条负面言论已存入\n"
                        f"用时 {elapsed_total} 秒",
                        parent=win,
                    ),
                    self.panel_negative._do_refresh(),
                ))
            run_async(_())
        threading.Thread(target=do, daemon=True).start()

    # ── 关注对象 ──

    def _add_watch(self):
        win = tk.Toplevel(self)
        win.title("添加关注对象")
        win.geometry("340x280")

        ttk.Label(win, text="平台").pack(anchor="w", padx=10, pady=5)
        pvar = tk.StringVar(value="抖音")
        ttk.Combobox(win, textvariable=pvar, values=list(CRAWLERS.keys()),
                     state="readonly").pack(padx=10, pady=5, fill="x")

        ttk.Label(win, text="昵称").pack(anchor="w", padx=10, pady=5)
        ename = ttk.Entry(win, width=30)
        ename.pack(padx=10, pady=5, fill="x")

        ttk.Label(win, text="平台号 / ID（选填，如抖音号）").pack(anchor="w", padx=10, pady=5)
        eid = ttk.Entry(win, width=30)
        eid.pack(padx=10, pady=5, fill="x")

        ttk.Label(win, text="提示：昵称和平台号至少填写一项",
                  foreground="gray").pack(anchor="w", padx=10)

        def save():
            p = pvar.get()
            tid = eid.get().strip()
            tn = ename.get().strip()
            if not tid and not tn:
                messagebox.showerror("错误", "请至少填写「昵称」或「平台号/ID」",
                                     parent=win)
                return
            target_id = tid or tn
            ok = run_async(database.add_watch_config(DB_PATH, p, target_id, tn))
            if ok:
                messagebox.showinfo("成功", "已添加", parent=win)
                win.destroy()
            else:
                messagebox.showerror("错误", "添加失败", parent=win)
        ttk.Button(win, text="保存", command=save).pack(pady=10)

    def _import_watch_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx *.xls")])
        if not path:
            return
        try:
            df = pd.read_excel(path)
            cols = df.columns.tolist()
            pc = "平台" if "平台" in cols else (cols[0] if cols else "")
            ic = ("ID" if "ID" in cols
                  else "item_id" if "item_id" in cols
                  else "平台号" if "平台号" in cols
                  else "")
            nc = ("昵称" if "昵称" in cols
                  else "nickname" if "nickname" in cols
                  else "")
            if not pc or (not ic and not nc):
                messagebox.showerror("错误",
                                     "Excel 需包含「平台」列，"
                                     "以及「ID」或「昵称」列中至少一列")
                return

            def do():
                cnt = 0
                for _, row in df.iterrows():
                    platform = str(row.get(pc, "")).strip()
                    tid = str(row.get(ic, "")).strip() if ic else ""
                    tname = str(row.get(nc, "")).strip() if nc else ""
                    if tid.lower() == "nan":
                        tid = ""
                    if tname.lower() == "nan":
                        tname = ""
                    target_id = tid or tname
                    if platform and target_id and platform in CRAWLERS:
                        run_async(database.add_watch_config(
                            DB_PATH, platform, target_id, tname))
                        cnt += 1
                self.after(0, lambda: messagebox.showinfo(
                    "完成", f"成功导入 {cnt} 个关注对象"))
            threading.Thread(target=do, daemon=True).start()
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _view_watch_list(self):
        """View and manage all watch targets."""
        win = tk.Toplevel(self)
        win.title("关注对象列表")
        win.geometry("600x420")

        btn_bar = ttk.Frame(win)
        btn_bar.pack(fill="x", padx=10, pady=(10, 4))

        def delete_selected():
            sel = tv.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选择要删除的关注对象", parent=win)
                return
            if not messagebox.askyesno("确认",
                                       f"确定删除选中的 {len(sel)} 个关注对象？",
                                       parent=win):
                return
            for iid in sel:
                vals = tv.item(iid, "values")
                config_id = int(vals[0])
                run_async(database.delete_watch_config_by_id(DB_PATH, config_id))
            load()
            messagebox.showinfo("完成", "已删除", parent=win)

        ttk.Button(btn_bar, text="删除选中", command=delete_selected).pack(side="left", padx=5)
        ttk.Button(btn_bar, text="全选", command=lambda: [
            tv.selection_add(iid) for iid in tv.get_children()
        ]).pack(side="left", padx=5)
        ttk.Button(btn_bar, text="刷新", command=lambda: load()).pack(side="left", padx=5)

        count_lbl = ttk.Label(btn_bar, text="", foreground="gray")
        count_lbl.pack(side="right", padx=5)

        tree_frame = ttk.Frame(win)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        tv = ttk.Treeview(tree_frame,
                          columns=("ID", "平台", "平台号/ID", "昵称", "添加时间"),
                          show="headings", height=14, selectmode="extended")
        tv.heading("ID", text="#")
        tv.heading("平台", text="平台")
        tv.heading("平台号/ID", text="平台号/ID")
        tv.heading("昵称", text="昵称")
        tv.heading("添加时间", text="添加时间")
        tv.column("ID", width=35)
        tv.column("平台", width=80)
        tv.column("平台号/ID", width=140)
        tv.column("昵称", width=140)
        tv.column("添加时间", width=140)

        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=scroll.set)
        tv.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def load():
            for iid in tv.get_children():
                tv.delete(iid)
            items = run_async(database.list_watch_config(DB_PATH))
            for item in items:
                tv.insert("", "end", values=(
                    item["id"], item["platform"], item["target_id"],
                    item.get("target_name", ""), item.get("created_at", ""),
                ))
            count_lbl.config(text=f"共 {len(items)} 个关注对象")

        load()

    def _download_watch_template(self):
        """Download a template Excel file for importing watch targets."""
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile="关注对象导入模板.xlsx",
        )
        if not path:
            return
        df = pd.DataFrame({
            "平台": ["抖音", "快手", "小红书", "微信视频号"],
            "ID": ["抖音号或留空", "快手号或留空", "", ""],
            "昵称": ["张三", "李四", "王五的昵称", "赵六的昵称"],
        })
        df.to_excel(path, index=False, engine="openpyxl")
        messagebox.showinfo("成功", f"模板已保存到:\n{path}\n\n"
                            "请按照模板格式填写后再导入。\n\n"
                            "「平台」列必须为：抖音、快手、小红书、微信视频号 之一\n"
                            "「ID」列为该平台的平台号（如抖音号），可留空\n"
                            "「昵称」列为对方昵称\n\n"
                            "ID 和昵称至少填写一项即可。")

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

    def _export_logs(self):
        """Pack all log files into a zip for sharing with tech support."""
        import shutil
        import tempfile
        from datetime import datetime as _dt
        log_dir = DATA_DIR / "logs"
        if not log_dir.exists() or not any(log_dir.iterdir()):
            messagebox.showwarning("提示", "暂无日志文件")
            return
        dest = filedialog.asksaveasfilename(
            defaultextension=".zip",
            filetypes=[("ZIP 压缩包", "*.zip")],
            initialfile=f"crawler_logs_{_dt.now():%Y%m%d_%H%M%S}",
            title="保存日志压缩包",
        )
        if not dest:
            return
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp) / "logs"
                shutil.copytree(log_dir, tmp_path)
                shutil.make_archive(str(Path(dest).with_suffix("")),
                                    "zip", tmp, "logs")
            messagebox.showinfo(
                "导出成功",
                f"日志已导出到：\n{dest}\n\n请将此文件发送给技术支持。")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _user_mgmt(self):
        win = tk.Toplevel(self)
        win.title("用户管理")
        win.geometry("500x400")

        btn_bar = ttk.Frame(win)
        btn_bar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Button(btn_bar, text="添加用户", command=lambda: self._add_user_dialog(win, load)).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="修改密码", command=lambda: self._change_pwd_dialog(win, tv)).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="删除用户", command=lambda: _delete()).pack(side="left", padx=3)
        count_lbl = ttk.Label(btn_bar, text="", foreground="gray")
        count_lbl.pack(side="right", padx=5)

        tv = ttk.Treeview(win, columns=("用户", "角色", "创建时间"), show="headings", height=12)
        tv.heading("用户", text="用户名")
        tv.heading("角色", text="角色")
        tv.heading("创建时间", text="创建时间")
        tv.column("用户", width=120)
        tv.column("角色", width=80)
        tv.column("创建时间", width=160)
        tv.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        def load():
            tv.delete(*tv.get_children())
            users = run_async(auth.list_users(DB_PATH))
            for u in users:
                tv.insert("", "end", values=(u["username"], u["role"], u.get("created_at", "")))
            count_lbl.config(text=f"共 {len(users)} 个用户")

        def _delete():
            sel = tv.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选中要删除的用户", parent=win)
                return
            uname = tv.item(sel[0])["values"][0]
            if uname == self.user["username"]:
                messagebox.showerror("错误", "不能删除自己", parent=win)
                return
            if not messagebox.askyesno("确认", f"确定删除用户「{uname}」？", parent=win):
                return
            ok, msg = run_async(auth.delete_user(DB_PATH, uname))
            if ok:
                messagebox.showinfo("成功", msg, parent=win)
                load()
            else:
                messagebox.showerror("错误", msg, parent=win)

        load()

    def _add_user_dialog(self, parent, refresh_cb=None):
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
                messagebox.showerror("错误", "请输入用户名和密码", parent=win)
                return
            ok, msg = run_async(auth.add_user(DB_PATH, u, p, rv.get()))
            if ok:
                messagebox.showinfo("成功", msg, parent=win)
                win.destroy()
                if refresh_cb:
                    refresh_cb()
            else:
                messagebox.showerror("错误", msg, parent=win)
        ttk.Button(win, text="添加", command=save).pack(pady=10)

    def _change_pwd_dialog(self, parent, tv):
        sel = tv.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中要修改密码的用户", parent=parent)
            return
        uname = tv.item(sel[0])["values"][0]
        win = tk.Toplevel(parent)
        win.title(f"修改密码 - {uname}")
        ttk.Label(win, text=f"为用户「{uname}」设置新密码：").pack(anchor="w", padx=10, pady=10)
        ep = ttk.Entry(win, width=25, show="*")
        ep.pack(padx=10, pady=5, fill="x")
        ttk.Label(win, text="确认新密码：").pack(anchor="w", padx=10, pady=5)
        ep2 = ttk.Entry(win, width=25, show="*")
        ep2.pack(padx=10, pady=5, fill="x")

        def save():
            p1, p2 = ep.get(), ep2.get()
            if not p1:
                messagebox.showerror("错误", "请输入新密码", parent=win)
                return
            if p1 != p2:
                messagebox.showerror("错误", "两次密码不一致", parent=win)
                return
            run_async(auth.change_password(DB_PATH, uname, p1))
            messagebox.showinfo("成功", f"用户「{uname}」密码已修改", parent=win)
            win.destroy()
        ttk.Button(win, text="确认修改", command=save).pack(pady=10)

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


def _check_browser_availability():
    """Verify that Chrome or Edge is available on the system.

    Shows a one-time warning if no browser is found — the tool can
    still run for data viewing/export but crawling will fail.
    """
    from .crawlers.browser_manager import _find_browser_executables
    import shutil

    found_browser = False

    if _find_browser_executables():
        found_browser = True

    if not found_browser:
        for cmd in ("google-chrome", "chromium-browser", "chrome", "msedge"):
            if shutil.which(cmd):
                found_browser = True
                break

    if not found_browser:
        try:
            import subprocess
            result = subprocess.run(
                ["playwright", "install", "--help"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                found_browser = True
        except Exception:
            pass

    if not found_browser:
        messagebox.showwarning(
            "浏览器提示",
            "未检测到 Google Chrome 或 Microsoft Edge 浏览器。\n\n"
            "采集功能需要系统中安装以下浏览器之一：\n"
            "• Google Chrome（推荐）\n"
            "• Microsoft Edge\n\n"
            "请安装后重启工具。\n"
            "其他功能（数据查看、导出等）仍可正常使用。",
        )


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_async(database.init_db(DB_PATH))
    run_async(auth.init_auth_db(DB_PATH))

    root = tk.Tk()
    root.title("爬虫小工具")
    root.geometry("1050x650")

    _check_browser_availability()

    LoginFrame(root, on_success=lambda user: _show_main(root, user)).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
