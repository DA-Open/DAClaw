# DAClaw Web Dashboard 云服务器临时测试部署指南

这份文档用于“第一阶段公网 IP 冒烟测试”。

适用场景：

- 你的服务器是一台全新的 Ubuntu 22.04
- 服务器上现在几乎什么都没有，还没有配环境
- 你准备通过 GitHub 把项目拉到服务器上
- 你当前只是想确认 DAClaw 的网页能不能先跑起来，并且能从你自己的电脑访问
- 你暂时还不处理域名、HTTPS、登录系统、正式生产部署这些事情

这是一份“临时测试部署”文档，不是生产环境部署方案。

## 你最终会得到什么

完成这份文档后，你应该可以从自己的电脑访问：

```text
http://<你的ECS公网IP>:9119/
http://<你的ECS公网IP>:9119/chat
```

其中：

- `/` 是 DAClaw 的 Web Dashboard 首页
- `/chat` 是聊天页面

## 先说清楚一个风险点

你现在这套 dashboard 不是一个已经做完公网安全隔离的 SaaS 前台，而是一个功能比较强的本地管理界面。

它不仅有聊天，还包含：

- 配置修改
- 环境变量管理
- 会话查看
- 日志查看
- 其他管理能力

所以这次测试时请尽量遵守下面几条：

- 安全组里优先只放行你自己电脑的公网 IP
- 不要长期对全网开放这个端口
- 不要把这套临时方案当成正式上线方案

## 开始前你需要准备的东西

请先确认你已经有：

- 一台带公网 IP 的阿里云 ECS
- 服务器系统为 Ubuntu 22.04
- 能通过 SSH 登录服务器
- 你这个 DAClaw 项目的 GitHub 仓库地址
- 你准备部署的准确 Git 分支名
- 你自己当前电脑的公网 IP，方便在安全组里做来源限制

如果你的 GitHub 仓库是私有仓库，还需要额外准备下面二选一：

- GitHub SSH key
- GitHub Personal Access Token

## 特别注意：你不是主分支

如果你的代码不在 `main`，不要直接按默认分支拉代码。

你现在这个本地工作树的分支是：

- `hermes_demo`

而且你还提到，后面也可能是基于 `hermes_demo` 再开的子分支。

所以部署时请始终拉“你当前真正要测试的那个分支”，例如：

- `hermes_demo`
- 或者某个从 `hermes_demo` 切出来的子分支

不要默认拉 `main`。

## 1. 先在阿里云安全组里放行端口

在 ECS 安全组入方向里，至少开放这两个端口：

- `22/tcp`，用于 SSH 登录
- `9119/tcp`，用于 DAClaw Web Dashboard

推荐的来源 CIDR：

- 你自己当前电脑的公网 IP，加 `/32`

例如：

```text
203.0.113.25/32
```

不建议一开始就使用：

```text
0.0.0.0/0
```

除非你只是做一个很短时间的临时测试，并且清楚这样会把端口暴露给所有公网来源。

## 2. 从你自己的电脑 SSH 登录服务器

在你本机终端执行：

```bash
ssh <你的用户名>@<你的ECS公网IP>
```

例如：

```bash
ssh root@47.95.242.210
```

或者：

```bash
ssh ubuntu@47.95.242.210
```

具体用户名以你的服务器实际设置为准。

## 3. 在 Ubuntu 22.04 上安装基础依赖

在服务器里执行：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv python3-pip tmux ufw
```

接着安装 Node.js 20。

你的 `web/` 前端使用的是较新的 Vite / React 依赖，Node 20 更稳妥：

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

安装完成后检查版本：

```bash
python3 --version
node -v
npm -v
git --version
```

## 4. 从 GitHub 拉取你要测试的分支

先创建一个工作目录：

```bash
mkdir -p ~/apps
cd ~/apps
```

### 4.1 如果你要部署的是 `hermes_demo`

直接执行：

```bash
git clone --branch hermes_demo --single-branch <你的GitHub仓库地址> DAClaw
cd DAClaw
```

例如：

```bash
git clone --branch hermes_demo --single-branch https://github.com/<你自己的账号>/<你的仓库名>.git DAClaw
cd DAClaw
```

如果你使用 SSH：

```bash
git clone --branch hermes_demo --single-branch git@github.com:<你自己的账号>/<你的仓库名>.git DAClaw
cd DAClaw
```

### 4.2 如果你要部署的是 `hermes_demo` 的某个子分支

把分支名替换成你的真实分支名：

```bash
git clone --branch <你的分支名> --single-branch <你的GitHub仓库地址> DAClaw
cd DAClaw
```

### 4.3 拉完后确认当前分支

进入仓库后执行：

```bash
git branch --show-current
```

你应该看到的是你刚刚指定的分支名，而不是 `main`。

### 4.4 如果你已经提前 clone 过仓库

那就不要重拉，直接切分支：

```bash
cd ~/apps/DAClaw
git fetch origin
git checkout <你的分支名>
git pull origin <你的分支名>
```

## 5. 创建 Python 虚拟环境

在仓库根目录执行：

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

安装 DAClaw 以及网页依赖：

```bash
pip install -e ".[web]"
```

注意：以后如果你重新开了一个新的 SSH 会话，需要先重新激活虚拟环境：

```bash
cd ~/apps/DAClaw
source venv/bin/activate
```

## 6. 构建前端页面

在仓库根目录执行：

```bash
cd web
npm install
npm run build
cd ..
```

这一步会把前端构建产物输出到：

```text
daclaw_cli/web_dist/
```

后面的 FastAPI dashboard 会直接把它作为静态 SPA 提供出来。

## 7. 启动 DAClaw Dashboard

第一次测试时，最简单的方法是直接前台运行，让 SSH 会话先保持打开：

```bash
cd ~/apps/DAClaw
source venv/bin/activate
daclaw dashboard --host 0.0.0.0 --port 9119 --insecure --no-open
```

也可以使用模块方式启动：

```bash
python -m daclaw_cli.main dashboard --host 0.0.0.0 --port 9119 --insecure --no-open
```

正常情况下，你应该会看到类似输出：

```text
DAClaw Web UI -> http://0.0.0.0:9119
```

这里有几个参数的含义：

- `--host 0.0.0.0`：允许从公网 IP 访问
- `--port 9119`：指定端口
- `--insecure`：允许非 localhost 绑定
- `--no-open`：服务器上不自动打开浏览器

## 8. 如果 Ubuntu 防火墙开启了，再放行 9119 端口

如果服务器上启用了 `ufw`，执行：

```bash
sudo ufw allow 9119/tcp
sudo ufw status
```

如果 `ufw` 当前是 inactive，也没关系。  
这个阶段最关键的仍然是阿里云安全组规则。

## 9. 在你自己的电脑上测试访问

打开你本机浏览器，访问：

```text
http://<你的ECS公网IP>:9119/
```

聊天页地址：

```text
http://<你的ECS公网IP>:9119/chat
```

例如：

```text
http://47.95.242.210:9119/
http://47.95.242.210:9119/chat
```

只要这个页面能成功打开，就已经说明下面这些都打通了：

- 云服务器网络正常
- 安全组配置基本正确
- Ubuntu 依赖基本正确
- DAClaw 后端能启动
- 前端构建产物能被正常提供

## 10. 如果页面能打开，但聊天不能正常回复

这通常不代表部署失败，而是说明：

- Web 服务已经起来了
- 但是模型、API Key 或运行配置还没有补齐

你现在这个阶段的目标只是“把页面跑起来、自己电脑能访问”，所以：

- 页面能打开 = 部署链路已经成功
- 聊天能正常回复 = 下一阶段再继续配置模型和密钥

## 11. 常用排查命令

### 11.1 检查服务是否真的监听在 9119

```bash
ss -ltnp | grep 9119
```

你希望看到的是：

- `0.0.0.0:9119`
- 或者 `*:9119`

如果只看到：

- `127.0.0.1:9119`

那说明它只监听本机，公网自然访问不到。

### 11.2 在服务器本机上自测

```bash
curl http://127.0.0.1:9119/
```

如果这个命令能通，而你自己的电脑打不开公网地址，最常见原因就是：

- 阿里云安全组没放行 `9119/tcp`
- Ubuntu 防火墙挡住了 `9119/tcp`
- 服务只绑定在 `127.0.0.1`

### 11.3 检查内存

```bash
free -h
```

### 11.4 看最近系统日志

```bash
dmesg | tail -n 50
```

如果进程被系统杀掉，通常这里能看到一些痕迹。

## 12. 可选：用 tmux 挂着运行

如果你不想一直保持 SSH 会话打开，可以用 `tmux`：

```bash
cd ~/apps/DAClaw
tmux new -s daclaw
source venv/bin/activate
daclaw dashboard --host 0.0.0.0 --port 9119 --insecure --no-open
```

启动后，按下面的组合键退出但不关闭进程：

```text
Ctrl+B，然后按 D
```

后面重新进入：

```bash
tmux attach -t daclaw
```

## 13. 测试完成后如何停止

如果你是前台运行：

```text
Ctrl+C
```

如果你是放在 `tmux` 里运行：

```bash
tmux attach -t daclaw
```

然后执行：

```text
Ctrl+C
```

## 14. 这个阶段完成之后，下一步做什么

当你确认自己的电脑已经能通过 ECS 公网 IP 打开这个页面后，下一步最合理的顺序是：

1. 配置一个能真正工作的模型
2. 配置对应的 API Key
3. 测试聊天是否正常返回
4. 再决定是否要进入“对外公开访问”的改造
5. 最后再处理域名、HTTPS、登录、数据库、权限拆分

## 命令速查版

如果你只是想快速照抄，最短命令流如下：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3 python3-venv python3-pip tmux ufw
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

mkdir -p ~/apps
cd ~/apps
git clone --branch <你的分支名> --single-branch <你的GitHub仓库地址> DAClaw
cd DAClaw

python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[web]"

cd web
npm install
npm run build
cd ..

sudo ufw allow 9119/tcp

source venv/bin/activate
daclaw dashboard --host 0.0.0.0 --port 9119 --insecure --no-open
```
