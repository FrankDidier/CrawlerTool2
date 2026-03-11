# 爬虫小工具

同城频道（抖音/快手/小红书/微信视频号）数据采集及数据处理工具。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装浏览器（Playwright 会自动下载 Chromium）
python -m playwright install chromium

# 3. 复制配置
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入 API Key 等（可选）

# 4. 运行
python main.py
```

**默认登录**：`admin` / `admin123`

## 使用流程

1. **登录**：使用 `admin` / `admin123` 登录
2. **平台登录**：点击右上角「设置」→ 在「平台登录」区域逐个登录各平台账号
   - 点击「登录」会打开浏览器窗口
   - 手动登录后关闭浏览器窗口，cookies 自动保存
3. **采集**：勾选平台 → 点击「开始采集」→ 自动循环采集
4. **数据管理**：在各面板中查看、搜索、导出、推送数据

## 项目结构

```
Crawl2/
├── main.py              # 启动入口
├── config.example.yaml  # 配置示例
├── requirements.txt
├── src/
│   ├── app.py           # 主界面
│   ├── auth.py          # 用户认证
│   ├── database.py      # 数据库
│   ├── llm.py           # 大模型语义分析
│   ├── notify.py        # 钉钉/微信推送
│   ├── export_utils.py  # 导出与备份
│   └── crawlers/        # 爬虫模块
│       ├── base.py
│       ├── browser_manager.py  # Playwright 浏览器管理
│       ├── manager.py
│       ├── douyin.py
│       ├── kuaishou.py
│       ├── xiaohongshu.py
│       └── wechat.py
├── data/                # 数据目录（自动创建）
├── 使用说明书.md
├── 部署教程.md
└── create_watch_template.py  # 生成关注对象导入模版
```

## 功能清单

| 功能 | 状态 |
|------|------|
| 4 平台同城采集（Playwright 浏览器自动化） | ✓ |
| 采集数据去重存储 | ✓ |
| 平台账号登录 & cookie 持久化 | ✓ |
| 用户登录（管理员/普通用户） | ✓ |
| 语义判断（大模型） | ✓ |
| 负面言论库 | ✓ |
| 关注对象提醒 | ✓ |
| Excel 导出（全部/选中） | ✓ |
| 钉钉/微信推送 | ✓ |
| 数据备份 | ✓ |

## 爬虫实现说明

各平台爬虫使用 **Playwright** 浏览器自动化技术：

1. 启动 headless Chromium 浏览器
2. 使用保存的 cookies 保持登录状态
3. 导航到各平台同城/探索页面
4. 通过网络拦截（`page.on("response")`）捕获 API 响应
5. 解析 JSON 数据为标准格式入库

**注意事项**：
- 采集前需先在「设置」中登录各平台（cookies 保存后无需重复登录）
- 各平台反爬策略可能变更，需定期维护
- 建议设置合理的采集间隔，避免触发风控
