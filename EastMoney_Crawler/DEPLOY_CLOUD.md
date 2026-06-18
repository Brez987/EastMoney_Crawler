# 云服务器部署指南 — 东方财富股吧全量爬取

> **服务器信息**
>
> | 项目 | 内容 |
> |------|------|
> | IP 地址 | **<YOUR_SERVER_IP>** |
> | 用户名 | **ubuntu** |
> | 密码 | **<YOUR_PASSWORD>** |
> | 登录端口 | **22** |
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

SSH 断开后爬虫不会停止：

```bash
# 新建 tmux 会话
tmux new -s guba_crawl

# 进入爬虫目录
cd ~/guba_crawler

# 运行爬虫
python3 main.py

# 按 Ctrl+B 然后按 D 断开但不停止
# 重新连接：tmux attach -t guba_crawl
# 列出会话：tmux ls
```

### 2.2 修改爬取参数

编辑 `main.py`：

```bash
nano main.py
```

修改股票代码和页码范围后保存（Ctrl+X, Y, Enter）。

### 2.3 查看爬取进度

爬虫会在终端实时打印进度信息：

```
000333: 已经成功爬取第 1 页帖子基本信息，进度 0.20%
000333: 已经成功爬取第 2 页帖子基本信息，进度 0.40%
...
000333: 第 660 页出现了错误 Message: ...
000333: 已经成功爬取第 661 页帖子基本信息，进度 99.80%
成功爬取 000333股吧共 500 页帖子，总计 40000 条，花费 45.23 分钟
```

---

## 3. 数据导出

### 3.1 从 MongoDB 导出

```bash
# 导出全部帖子数据
mongodump --db post_info --out ~/guba_data_export/

# 导出全部评论数据
mongodump --db comment_info --out ~/guba_data_export/

# 导出单只股票数据
mongodump --db post_info --collection post_000333 --out ~/guba_data_export/
```

### 3.2 下载到本地

```bash
# 在本地终端执行
scp -P 22 -r ubuntu@<YOUR_SERVER_IP>:~/guba_data_export/ /Users/text/Desktop/
```

### 3.3 查看数据统计

```bash
mongosh --quiet --eval "
  db = db.getSiblingDB('post_info');
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

## 5. 常见问题

> **Q: Chrome 无法启动？**
> A: 检查是否安装了必要的库：`sudo apt install -y libx11-xcb1 libxcomposite1 libxdamage1 libxi6 libxtst6 libnss3 libnspr4 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libpango-1.0-0`

> **Q: 爬着爬着就停了？**
> A: 检查 tmux 是否还在运行，查看是否有错误输出。可能是 Chrome 崩溃，代码会自动重启。

> **Q: MongoDB 连不上？**
> A: 检查 MongoDB 进程：`sudo systemctl status mongod`

> **Q: 磁盘满了？**
> A: MongoDB 数据可能占用过多空间。定期导出并清理：`sudo systemctl stop mongod && sudo rm -rf /var/lib/mongodb/* && sudo systemctl start mongod`
