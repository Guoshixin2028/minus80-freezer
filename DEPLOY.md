# 正式公网部署指南（Render + Neon，全程免费）

> 目标：获得一个**固定 HTTPS 域名**，任何人任何网络都能打开管理台，数据永久保存在云端数据库，不再依赖任何人的电脑。
>
> 全程只需注册 2 个免费账号 + 复制粘贴，约 20 分钟。需要帮忙随时找 WorkBuddy。

## 为什么选这套方案

| 方案 | 固定域名 | 数据安全 | 依赖本机开机 | 费用 |
|------|---------|---------|------------|------|
| 花生壳隧道（旧） | ❌ 每次重启都变 | 一般 | ✅ 必须 | 免费 |
| **Render + Neon（本方案）** | ✅ 永久不变 | ✅ 云端数据库 | ❌ 不依赖 | 免费 |

## 第 0 步：准备（本机已完成 ✅）

- [x] `app.py` 已升级支持 Postgres（设置环境变量 `DATABASE_URL` 即启用；不设置仍用本地 SQLite，行为不变）
- [x] `requirements.txt` 已加 `psycopg[binary]`
- [x] `render.yaml` 部署配置已生成
- [x] 本地 `data/export-latest.json` 已导出当前全部数据（rev=5，含 2 个铁架），部署完成后导入即可

## 第 1 步：注册 GitHub 并上传代码

1. 注册 / 登录 [github.com](https://github.com)（邮箱即可）
2. 新建仓库：右上角 **+** → New repository → 名称 `minus80-freezer` → **Private** → Create
3. 在本机 `server` 目录执行（把 `你的用户名` 换成你的 GitHub 用户名）：

```bash
cd "E:/learning/project/IGEM/daylab/software/-80度菌名统计/server"
git remote add origin https://github.com/你的用户名/minus80-freezer.git
git push -u origin main
```

> 本地仓库已初始化并提交完毕（`data/` 本地数据库、`__pycache__` 已通过 `.gitignore` 排除，不会上传）。

## 第 2 步：注册 Neon，创建免费数据库

1. 打开 [neon.com](https://neon.com) → **Sign Up**（可用 GitHub 账号直接登录）
2. 创建项目：Project name 填 `freezer`，Region 选 **Singapore (ap-southeast-1)**（离国内最近）→ Create
3. 在项目首页找到 **Connection string**，点复制，形如：

```
postgresql://user:password@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

> 建议复制带 **`-pooler`** 字样的那条（连接池模式，对休眠唤醒更友好）。
> 这串就是 `DATABASE_URL`，先存到记事本，**不要发给任何人**（等于数据库密码）。

## 第 3 步：注册 Render，部署后端

1. 打开 [render.com](https://render.com) → **Get Started** → 用 GitHub 账号登录（会要求授权，同意即可）
2. Dashboard → **New +** → **Web Service** → 选择刚推送的 `minus80-freezer` 仓库 → Connect
3. 按下面填写：

| 配置项 | 填写内容 |
|--------|---------|
| Name | `minus80-freezer` |
| Region | **Singapore (Southeast Asia)** |
| Branch | `main` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Free** |

4. 展开左下角 **Advanced** → **Add Environment Variable**：
   - Key：`DATABASE_URL`
   - Value：第 2 步复制的 Neon 连接串
5. 点 **Create Web Service**，等 3~5 分钟构建完成
6. 页面顶部会出现固定域名：**`https://minus80-freezer.onrender.com`** ← 这就是永久官网地址！

## 第 4 步：导入现有数据

1. 手机/电脑打开 `https://minus80-freezer.onrender.com`（首次打开要等 30~60 秒，免费实例冷启动，属正常）
2. 进入左侧菜单「数据与统计」→ **导入** → 选择本文件旁边的 `data/export-latest.json` → 确认
3. 铁架、盒子、菌株、取出历史全部恢复 ✅

## 第 5 步：更新 NFC 手环

用 NFC Tools 重写两条手环的 URL（把域名换成新地址）：

| 手环 | URL |
|------|-----|
| 存环 | `https://minus80-freezer.onrender.com/?view=store&rack=铁架 1` |
| 取环 | `https://minus80-freezer.onrender.com/?view=search` |

之后就可以把官网链接挂到队伍官网上，任何人点开即用。

## 日常维护

- **改代码后更新网站**：改完 → `git add -A && git commit -m "说明" && git push` → Render 自动重新部署（约 3 分钟）
- **数据备份**：网页顶栏「导出备份」随时下载 JSON；后端每次保存还会自动在数据库里留最近 200 份快照
- **免费额度**：Render 免费 750 小时/月（单个服务够用）；Neon 免费 0.5GB（够存几万条记录）

## 常见问题

- **首次打开很慢？** 免费实例 15 分钟无人访问会休眠，下次访问需冷启动 30~60 秒，数据不受影响。实验室高频使用时基本无感。
- **国内访问偏慢/打不开？** onrender.com 在个别网络环境下不稳定。若实验室访问困难：① 换手机流量试；② 联系 WorkBuddy 切换备用方案（Railway / 域名 + CDN）。
- **数据会丢吗？** 不会。数据存在 Neon 数据库里，Render 重新部署、休眠、重启都不影响；Neon 长时间无人访问会自动暂停，下次连接自动唤醒。
