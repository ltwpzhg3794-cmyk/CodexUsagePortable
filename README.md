# Codex 便携用量条

在 Windows 版 Codex 的输入框工具栏旁显示当前聊天的累计 Token、本轮 Token、缓存命中率和估算输出速度（TPS）。程序从本地 Codex 日志和会话记录读取数据，无需 API 密钥。

## 下载与使用

1. 在 [Releases](../../releases/latest) 下载 `CodexUsagePortable.exe`（Windows x64）。
2. 打开 Codex 桌面版和一个聊天，再双击运行 EXE。
3. 右键点击系统托盘里的用量条图标，可显示、移除用量条或退出。

程序默认在用户目录的 `.codex` 中读取会话记录。如果你把 Codex 数据放在了别处，请在运行前将 `CODEX_HOME` 指向该目录。用量暂未写入日志时，悬浮条会显示等待状态。输入框空间不足时，可能只显示部分指标。

悬浮条依赖 Codex 的 Windows 窗口布局和本地日志格式，Codex 更新后可能需要适配。

## 从源码运行或打包

在 Windows x64 上安装带 Tkinter 的 Python 3.12，然后在仓库目录中运行下面的命令。

```powershell
py -3.12 -m venv .venv
\.venv\Scripts\python.exe -m pip install -r requirements.txt
\.venv\Scripts\python.exe portable.py
```

用下面的命令生成 `dist\CodexUsagePortable.exe`。

```powershell
\.venv\Scripts\python.exe -m pip install pyinstaller==6.22.3
\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --noupx --name CodexUsagePortable --icon tray.ico --add-data 'tray.ico:.' --collect-submodules comtypes portable.py
```
