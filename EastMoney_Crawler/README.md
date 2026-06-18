# 云服务器部署指南 — 东方财富股吧全量爬取

> **服务器信息**
>
> | 项目 | 内容 |
> |------|------|
> | IP 地址 | **\<YOUR_SERVER_IP\>** |
| 用户名 | **ubuntu** |
| 密码 | **\<YOUR_PASSWORD\>** |
| 登录端口 | **22** |
> | 用途 | 全量爬取东方财富股吧数据 |
> | 到期时间 | 2026年4月 |

---

## 1. 首次部署

### 1.1 登录服务器

```bash
ssh ubuntu@<YOUR_SERVER_IP> -p 22
# 密码: <YOUR_PASSWORD>
```

### 1.2 一键环境安装

登录后依次执行以下命令：

```bash
# 系统更新
sudo apt update && sudo apt upgrade -y

# Python 环境
sudo apt install python3 python3-pip python3-venv -y

# MongoDB 安装
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
  sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg
echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] http://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | \
  sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update
sudo apt install mongodb-org -y
sudo systemctl start mongod
sudo systemctl enable mongod

# Python 依赖
pip3 install selenium pymongo pandas requests tqdm
```

### 1.3 安装 Chrome + ChromeDriver

```bash
# 下载 Chrome for Testing
wget https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/linux64/chrome-linux64.zip
unzip chrome-linux64.zip
sudo mv chrome-linux64 /opt/
sudo ln -sf /opt/chrome-linux64/chrome /usr/local/bin/google-chrome

# 下载 ChromeDriver
wget https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/linux64/chromedriver-linux64.zip
unzip chromedriver-linux64.zip
sudo mv chromedriver-linux64 /opt/
sudo ln -sf /opt/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver

# 验证
google-chrome --version
chromedriver --version
```

### 1.4 上传代码到服务器

在本地终端（非服务器）执行：

```bash
scp -P 22 -r \Echo_Chambers\股吧爬虫代码\EastMoney_Crawler/ \
  ubuntu@<YOUR_SERVER_IP>:~/guba_crawler/
```

### 1.5 创建 MongoDB 数据库

```bash
mongosh
use post_info
use comment_info
exit
```

---

## 2. 运行爬虫

### 2.1 使用 tmux 保持运行

SSH 断开后爬虫不会停止。推荐创建多个 tmux 窗口分别执行不同任务：

```bash
# 新建 tmux 会话
tmux new -s guba_crawl

# 进入爬虫目录
cd ~/guba_crawler/EastMoney_Crawler

# 按 Ctrl+B 然后按 C 创建新窗口
# 按 Ctrl+B 然后按 0/1/2 切换窗口
```

**窗口 0 — 爬取帖子列表（含财富号帖子）**

```bash
# 编辑 main.py，启用帖子列表爬取
nano main.py
```

修改 `main.py` 中 `if __name__ == "__main__":` 部分：

```python
if __name__ == "__main__":
    # 步骤 1：爬取发帖信息（第 1-50 页）
    thread1 = threading.Thread(target=post_thread, args=('601138', 1, 50))
    thread1.start()
    thread1.join()
    print(f"you have fetched data successfully, congratulations!")
```

运行：
```bash
python3 main.py
```

**窗口 1 — 爬取帖子正文（财富号帖子）**

帖子列表爬取完成后，修改 `main.py` 爬取正文：

```python
if __name__ == "__main__":
    # 步骤 2：爬取帖子正文内容（财富号帖子）
    # url_type 参数：'caifuhao' 仅财富号, 'guba' 仅股吧原生, 'all' 所有帖子
    thread1 = threading.Thread(target=post_detail_thread, args=('601138', None, 'caifuhao'))
    thread1.start()
    thread1.join()
    print(f"you have fetched data successfully, congratulations!")
```

运行：
```bash
python3 main.py
```

**窗口 2 — 爬取评论信息**

```python
if __name__ == "__main__":
    # 步骤 3：爬取评论信息（需先完成帖子列表爬取）
    thread1 = threading.Thread(target=comment_thread_date, args=('601138', '2025-01-01', '2026-06-06'))
    thread1.start()
    thread1.join()
    print(f"you have fetched data successfully, congratulations!")
```

**tmux 常用操作**

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+B` 然后 `D` | 断开会话（后台继续运行） |
| `Ctrl+B` 然后 `C` | 创建新窗口 |
| `Ctrl+B` 然后 `0/1/2` | 切换到指定窗口 |
| `Ctrl+B` 然后 `%` | 垂直分屏 |
| `Ctrl+B` 然后 `"` | 水平分屏 |

```bash
# 重新连接会话
tmux attach -t guba_crawl

# 列出所有会话
tmux ls

# 关闭会话
tmux kill-session -t guba_crawl
```

### 2.2 修改爬取参数

编辑 `main.py`：

```bash
nano ~/guba_crawler/EastMoney_Crawler/main.py
```

**关键参数说明：**

| 参数 | 示例 | 说明 |
|------|------|------|
| `stock_symbol` | `'601138'` | 股票代码 |
| `start_page` | `1` | 起始页码 |
| `end_page` | `50` | 结束页码 |
| `limit` | `None` | 正文爬取数量限制，None 表示全部 |
| `url_type` | `'caifuhao'` | 帖子类型筛选：`'all'`/`'caifuhao'`/`'guba'` |

保存（Ctrl+X, Y, Enter）。

### 2.3 查看爬取进度

爬虫会在终端实时打印进度信息：

```
601138: 已经成功爬取第 1 页帖子基本信息，进度 2.00%
601138: 已经成功爬取第 2 页帖子基本信息，进度 4.00%
...
601138: 开始爬取 222 条帖子的正文内容...
601138: 已爬取第 1/222 条帖子正文，进度 0.45%
601138: 已爬取第 5/222 条帖子正文，进度 2.25%
601138: 已爬取 5 条帖子，主动重启浏览器...
...
601138: 正文爬取完成，共处理 222 条帖子
```

---

## 2.4 000001 自动化流水线（CSV-Native 高速版）

> 针对个股 **000001** 的完整自动化流程：提取历史数据 → 补爬正文 → 爬取新帖 → 整合 → 上传百度网盘 → 清理空间。
> 只需创建 **3 个 PowerShell 窗口**，按顺序运行三个阶段即可。

### 优化原理

旧版流程需要将 **24 万+ 条历史记录导入 MongoDB**（耗时 10 分钟以上）。新版改为 **CSV-Native 全流程**：

| 环节 | 旧版 | 新版（CSV-Native） |
|------|------|-------------------|
| 去重 | 查询 MongoDB | 内存 `set`（1.4 秒加载 24 万 ID） |
| 新帖存储 | `insert_many` 到 MongoDB | 直接追加到 CSV 文件 |
| 正文更新 | `update_one` 到 MongoDB | 流式重写 CSV（内存占用极低） |
| 导出 | 从 MongoDB 读取再写 CSV | 直接合并 CSV 文件 |

**提速效果**：Stage 1 准备时间从 **~15 分钟** 降至 **~2 秒**。

### 前置要求

**1. 确保依赖已安装：**

```powershell
pip install pymongo selenium pandas tqdm beautifulsoup4 requests
```

**2. （可选）百度网盘上传（Stage 3 需要，可跳过）：**

```bash
bypy info
# 应显示你的网盘容量信息
```

### 阶段 1：提取历史数据 + 爬取帖子列表

打开第一个 PowerShell 窗口，运行数据准备和帖子列表爬取：

```powershell
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 1
```

该阶段自动完成：
1. 优先读取项目目录下 `000001.csv`（无 `数据.rar` 时回退本地 CSV）
2. **读取所有 `post_id` 到内存 `set`**（约 1.2 秒，244K 条记录）
3. **分析 CSV 最新帖子日期**（如 `2025-01-22`），作为增量补爬的停止阈值
4. 从第 1 页开始爬取帖子列表，**当一页中所有帖子日期 <= CSV 最新日期时自动停止**，只保留 CSV 截止日期之后的**新帖子**
5. 新帖子直接追加到 CSV，旧数据不导入 MongoDB，省去大量 I/O 时间

> 保持窗口打开即可，爬虫在后台运行。

### 阶段 2：爬取正文

**等 Stage 1 完成后**（观察窗口输出或检查标记文件），打开第二个 PowerShell 窗口：

```powershell
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 2

# 或自定义并发 worker 数（默认 3）
python auto_pipeline_000001.py --stock 000001 --stage 2 --detail-workers 4
```

该阶段自动完成：
1. 从 CSV 筛选所有 **`content` 为空** 的帖子
2. **普通股吧帖**：content 直接用 `post_title` 填充（本地秒完成，不爬详情页）
3. **财富号帖子**：走多线程 requests 补爬完整正文
4. 正文通过**增量 JSONL** 持久化（断点续爬友好），最后统一写回 CSV
5. **不爬评论，不需要 MongoDB**

### 阶段 3：整合 + 导出到 data/

**等 Stage 2 完成后**，打开第三个 PowerShell 窗口：

```powershell
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 3
```

该阶段自动完成：
1. **按时间降序合并** 基础 CSV + 新帖子 CSV → `000001_enhanced.csv`（新帖在前）
2. **复制到 `data/` 目录**（`SKIP_BAIDU_UPLOAD=True` 本地检查模式，跳过上传和清理）
3. 不导出评论 CSV

> 检查完毕后，将 `auto_pipeline_000001.py` 中的 `SKIP_BAIDU_UPLOAD` 改为 `False` 即可恢复百度网盘上传 + 清理流程。

### 流水线命令速查

```powershell
# ====== 分别在新 PowerShell 窗口运行三个阶段 ======
# 窗口 1: Stage 1
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 1

# 窗口 2: Stage 2（等 Stage 1 完成）
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 2

# 窗口 2（自定义并发数，默认 3）：
python auto_pipeline_000001.py --stock 000001 --stage 2 --detail-workers 4

# 窗口 3: Stage 3（等 Stage 2 完成）
cd e:\guba_project\EastMoney_Crawler
python auto_pipeline_000001.py --stock 000001 --stage 3

# ====== 检查阶段标记 ======
dir e:\guba_project\EastMoney_Crawler\.pipeline_flags\
# 看到 000001_stage1.done 表示阶段1已完成

# ====== 查看网盘上传结果（需 bypy） ======
bypy list /apps/bypy/guba_crawl/000001/
```

### 切换到下一只股票

000001 处理完成后，将新股票的 `.csv` 文件放到 `EastMoney_Crawler\` 目录下，然后通过 `--stock` 指定股票代码，无需修改源码：

```powershell
python auto_pipeline_000001.py --stock 000002 --stage 1
python auto_pipeline_000001.py --stock 000002 --stage 2
python auto_pipeline_000001.py --stock 000002 --stage 3
```

然后重新按 Stage 1 → Stage 2 → Stage 3 的顺序执行。

---

## 3. 数据查看与导出

### 3.1 连接 MongoDB 查看数据

**方式一：使用 mongosh 交互式命令行**

```bash
# 进入 MongoDB Shell
mongosh

# 选择数据库
use post_info

# 查看所有集合（股票）
show collections

# 查看某只股票帖子总数
db.post_601138.countDocuments()

# 查看前 5 条帖子
db.post_601138.find().limit(5)

# 查看财富号帖子（URL 包含 caifuhao）
db.post_601138.find({post_url: {$regex: 'caifuhao'}}).limit(5)

# 查看有正文的帖子
db.post_601138.find({post_content: {$exists: true, $ne: ''}}).limit(5)

# 查看指定帖子的完整内容
db.post_601138.findOne({_id: '20260606080052249419730'})

# 退出
exit
```

**方式二：使用 Python 脚本查询**

创建查询脚本 `query_data.py`：

```python
from pymongo import MongoClient

client = MongoClient('localhost', 27017)
db = client['post_info']
collection = db['post_601138']

# 统计总帖子数
total = collection.count_documents({})
print(f'总帖子数: {total}')

# 统计财富号帖子数
caifuhao_count = 0
for post in collection.find({}, {'post_url': 1}):
    if 'caifuhao' in post['post_url']:
        caifuhao_count += 1
print(f'财富号帖子数: {caifuhao_count}')

# 查看有正文的帖子数
has_content = collection.count_documents({'post_content': {'$exists': True, '$ne': ''}})
print(f'有正文的帖子数: {has_content}')

# 查看最新 3 条财富号帖子
print('\n=== 最新财富号帖子 ===')
for post in collection.find({}, {'_id': 1, 'post_title': 1, 'post_url': 1, 'post_content': 1}).sort('_id', -1).limit(3):
    if 'caifuhao' in post['post_url']:
        content_len = len(post.get('post_content', ''))
        print(f"\n标题: {post['post_title']}")
        print(f"ID: {post['_id']}")
        print(f"正文长度: {content_len} 字")
```

运行：
```bash
cd ~/guba_crawler/EastMoney_Crawler
python3 query_data.py
```

### 3.2 从 MongoDB 导出数据

**导出为 BSON（完整备份）**

```bash
# 创建导出目录
mkdir -p ~/guba_data_export

# 导出全部帖子数据
mongodump --db post_info --out ~/guba_data_export/

# 导出全部评论数据
mongodump --db comment_info --out ~/guba_data_export/

# 导出单只股票数据
mongodump --db post_info --collection post_601138 --out ~/guba_data_export/

# 压缩导出数据
cd ~ && tar -czvf guba_data_export.tar.gz guba_data_export/
```

**导出为 JSON（便于查看和处理）**

```bash
# 导出全部帖子为 JSON
mongoexport --db post_info --collection post_601138 --out ~/guba_data_export/post_601138.json

# 导出指定字段（标题、正文、URL）
mongoexport --db post_info --collection post_601138 \
  --fields "post_title,post_content,post_url,post_date,post_author,post_view,comment_num" \
  --out ~/guba_data_export/post_601138_fields.json

# 导出财富号帖子（通过 Python 脚本过滤）
python3 -c "
from pymongo import MongoClient
import json

client = MongoClient('localhost', 27017)
db = client['post_info']
collection = db['post_601138']

posts = []
for post in collection.find({}, {'_id': 0}):
    if 'caifuhao' in post.get('post_url', ''):
        posts.append(post)

with open('~/guba_data_export/caifuhao_601138.json', 'w', encoding='utf-8') as f:
    json.dump(posts, f, ensure_ascii=False, indent=2)

print(f'导出 {len(posts)} 条财富号帖子')
"
```

**导出为 CSV（便于 Excel 分析）**

```bash
# 安装 mongoexport 的 CSV 支持（如需要）
# 使用 Python 导出 CSV
python3 -c "
from pymongo import MongoClient
import csv

client = MongoClient('localhost', 27017)
db = client['post_info']
collection = db['post_601138']

with open('~/guba_data_export/post_601138.csv', 'w', newline='', encoding='utf-8-sig') as f:
    writer = csv.writer(f)
    writer.writerow(['ID', '标题', '作者', '日期', 'URL', '正文长度'])
    
    for post in collection.find({}, {'_id': 1, 'post_title': 1, 'post_author': 1, 'post_date': 1, 'post_url': 1, 'post_content': 1}):
        content_len = len(post.get('post_content', ''))
        writer.writerow([
            post['_id'],
            post.get('post_title', ''),
            post.get('post_author', ''),
            post.get('post_date', ''),
            post['post_url'],
            content_len
        ])

print('CSV 导出完成')
"
```

### 3.3 下载到本地

```bash
# 在本地终端执行（Mac/Linux）
scp -P 22 ubuntu@<YOUR_SERVER_IP>:~/guba_data_export/guba_data_export.tar.gz ~/Desktop/

# 解压
 tar -xzvf ~/Desktop/guba_data_export.tar.gz -C ~/Desktop/

# Windows 用户可使用 WinSCP 或 FileZilla 下载
```

### 3.4 查看数据统计

```bash
# 快速统计所有集合的文档数
mongosh --quiet --eval "
  db = db.getSiblingDB('post_info');
  print('=== 帖子数据 ===');
  db.getCollectionNames().forEach(c => {
    var count = db.getCollection(c).countDocuments();
    var hasContent = db.getCollection(c).countDocuments({post_content: {$exists: true, $ne: ''}});
    print(c + ': ' + count + ' 条（含正文: ' + hasContent + ' 条）');
  });
"

# 统计评论数据
mongosh --quiet --eval "
  db = db.getSiblingDB('comment_info');
  print('\n=== 评论数据 ===');
  db.getCollectionNames().forEach(c => {
    print(c + ': ' + db.getCollection(c).countDocuments() + ' 条');
  });
"
```

---

## 4. 运维 Tips

### 4.1 监控磁盘空间

```bash
# 检查磁盘使用
df -h

# 检查 MongoDB 数据大小
du -sh /var/lib/mongodb/
```

### 4.2 查看 MongoDB 日志

```bash
sudo tail -f /var/log/mongodb/mongod.log
```

### 4.3 重启 MongoDB

```bash
sudo systemctl restart mongod
```

### 4.4 多终端管理

```bash
# 开多个 tmux 窗口跑不同任务
tmux new -s crawl_post    # 跑帖子爬取
tmux new -s crawl_comment  # 跑评论爬取
tmux new -s monitor        # 监控资源
```

---

## 4. 运维 Tips

### 4.1 监控磁盘空间

```bash
# 检查磁盘使用
df -h

# 检查 MongoDB 数据大小
du -sh /var/lib/mongodb/

# 检查导出目录大小
du -sh ~/guba_data_export/
```

### 4.2 查看 MongoDB 日志

```bash
sudo tail -f /var/log/mongodb/mongod.log
```

### 4.3 重启 MongoDB

```bash
sudo systemctl restart mongod
```

### 4.4 多终端管理

```bash
# 开多个 tmux 窗口跑不同任务
tmux new -s crawl_post      # 跑帖子列表爬取
tmux new -s crawl_detail    # 跑帖子正文爬取
tmux new -s crawl_comment   # 跑评论爬取
tmux new -s monitor         # 监控资源
```

---

## 5. 常见问题

### 5.1 环境相关问题

> **Q: Chrome 无法启动？**
> A: 检查是否安装了必要的库：
> ```bash
> sudo apt install -y libx11-xcb1 libxcomposite1 libxdamage1 libxi6 libxtst6 libnss3 libnspr4 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libpango-1.0-0
> ```

> **Q: MongoDB 连不上？**
> A: 检查 MongoDB 进程：
> ```bash
> sudo systemctl status mongod
> sudo systemctl start mongod
> ```

> **Q: 磁盘满了？**
> A: MongoDB 数据可能占用过多空间。定期导出并清理：
> ```bash
> sudo systemctl stop mongod && sudo rm -rf /var/lib/mongodb/* && sudo systemctl start mongod
> ```

### 5.2 爬虫运行问题

> **Q: 爬着爬着就停了？**
> A: 检查 tmux 是否还在运行，查看是否有错误输出。可能是 Chrome 崩溃，代码会自动重启浏览器。如果频繁停止，尝试降低爬取页数或增加延迟。

> **Q: 正文爬取时提示"页面被重定向到验证页"？**
> A: 这是东方财富的反爬机制。代码已自动处理：
> - 每爬取 5 条帖子会自动重启浏览器
> - 连续 3 次错误会强制重启并增加延迟
> - 如果仍被拦截，建议暂停 10-15 分钟后再继续

> **Q: 财富号帖子正文为空？**
> A: 检查是否已完成以下步骤：
> 1. 先运行 `post_thread` 爬取帖子列表
> 2. 再运行 `post_detail_thread` 爬取正文
> 3. 确认 `url_type` 参数设置为 `'caifuhao'` 或 `'all'`

> **Q: 如何只爬取股吧原生帖子（不爬财富号）？**
> A: 修改 `main.py`：
> ```python
> thread1 = threading.Thread(target=post_detail_thread, args=('601138', None, 'guba'))
> ```
> 但注意：股吧原生帖子直接访问容易被验证页拦截，成功率较低。

> **Q: 如何重新爬取已完成的股票？**
> A: 先清空该股票的数据：
> ```bash
> mongosh --eval "db.post_601138.drop()" post_info
> ```
> 然后重新运行爬虫。

### 5.3 数据相关问题

> **Q: MongoDB 中 `_id` 是什么格式？**
> A: 字符串类型。对于股吧原生帖子，`_id` 从 URL 提取（如 `'1721833409'`）；对于财富号帖子，`_id` 是 URL 中的数字部分（如 `'20260606080052249419730'`）。

> **Q: 如何确认财富号帖子已正确入库？**
> A: 使用 Python 脚本查询：
> ```bash
> python3 -c "
> from pymongo import MongoClient
> client = MongoClient('localhost', 27017)
> db = client['post_info']
> all_posts = list(db['post_601138'].find({}, {'post_url': 1}))
> caifuhao = [p for p in all_posts if 'caifuhao' in p['post_url']]
> print(f'财富号帖子数: {len(caifuhao)} / {len(all_posts)}')
> "
> ```

> **Q: 导出的 JSON 文件中文显示乱码？**
> A: 确保导出时指定 UTF-8 编码，并使用支持 UTF-8 的编辑器（如 VS Code、Notepad++）打开。
