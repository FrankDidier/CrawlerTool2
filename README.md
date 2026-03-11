# 爬虫小工具

同城频道（抖音/快手/小红书/微信视频号）数据采集及数据处理工具。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制配置
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入 API Key 等（可选）

# 3. 运行
python main.py
```

**默认登录**：`admin` / `admin123`

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
| 4 平台同城采集框架 | ✓ |
| 采集数据去重存储 | ✓ |
| 用户登录（管理员/普通用户） | ✓ |
| 语义判断（大模型） | ✓ |
| 负面言论库 | ✓ |
| 关注对象提醒 | ✓ |
| Excel 导出 | ✓ |
| 钉钉/微信推送 | ✓ |
| 数据备份 | ✓ |

## 爬虫实现说明

当前各平台爬虫（`src/crawlers/*.py`）为**占位实现**，需根据各平台当前接口补充真实爬取逻辑。建议：

1. 接入官方开放接口（如有）
2. 使用合规第三方数据服务
3. 或自行研究各平台同城接口并实现

反爬策略变更时需相应维护。
