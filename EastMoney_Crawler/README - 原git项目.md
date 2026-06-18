# 东方财富股吧爬虫 — 最终交付包

> 本文件夹是完整的股吧全量爬取代码交付包。

---

## 文件夹结构

```
股吧爬虫代码/
│
├── EastMoney_Crawler/          ← ★ 核心交付：爬虫代码 + 完整技术文档
│   ├── README.md               ← 技术文档（代码说明/爬取逻辑/反爬/待扩展）
│   ├── DEPLOY_CLOUD.md         ← 云服务器部署指南
│   ├── requirements.txt        ← Python 依赖
│   ├── main.py / crawler.py / parser.py / ...  ← 源代码
│   └── tests/                  ← 单元测试
│
├── missing_list_2009_2025.csv  ← 缺失年份清单（补爬工具的数据输入）
│
└── _原始资料/                   ← 原始需求文档（仅供查阅，无需修改）
    ├── EastMoney_Crawler.zip          ← 代码原始压缩包（备份）
    ├── 【v3.0】东方财富股吧数据爬取需求说明.docx  ← 原始需求文档
    ├── 股吧列表页数据爬取需求说明.pdf          ← 原始需求文档
    └── 股吧爬虫项目文档v2.0.rar              ← 旧版项目文档
```

## 使用指引

| 你要做什么 | 看哪个文件 |
|-----------|-----------|
| 了解整体爬虫逻辑和代码架构 | `EastMoney_Crawler/README.md` |
| 在服务器上部署运行 | `EastMoney_Crawler/DEPLOY_CLOUD.md` |
| 安装依赖 | `EastMoney_Crawler/requirements.txt` |
| 查看原始需求文档（历史存档） | `_原始资料/` 下的文件 |

## 说明

- `_原始资料/` 中的文件是历史原始文档，仅作为参考存档，**核心参考以 `EastMoney_Crawler/README.md` 为准**
- `missing_list_2009_2025.csv` 是缺失年份补爬工具的数据输入文件，需要放在代码同级目录使用
