# 开发与打包

用户操作见 [README](../README.md)。开发环境为 Python 3.11+，运行依赖见 `requirements.txt`；Windows 集成是主要验证范围。

## 目录

| 目录 / 文件 | 用途 |
| --- | --- |
| `main.py` | 程序入口 |
| `api/providers/` | 各平台额度解析与凭据读取 |
| `api/transport.py` | 平台主机限制、独立请求会话 |
| `ui/` | 悬浮窗、详情面板、账号与高级设置 |
| `utils/account_store.py` | 账号元数据与系统凭据库 |
| `utils/app_settings.py` | 用户配置、图片与导入导出 |
| `utils/proxy_config.py` | 全平台共用代理 |
| `config/settings.toml` | 随源码分发的默认值，不放个人配置 |
| `data/` | 运行数据，不提交、不分发 |

用户设置保存在 `data/settings.toml`，覆盖内置默认值。代理保存在 `data/proxy.toml`。源码运行时 `data/` 位于项目根目录，打包后位于 EXE 同级目录；不要编辑 `_internal` 来设置个人偏好。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe tests/test_dock_layout.py
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
git diff --check
```

测试使用临时目录、假凭据和模拟网络。自动化通过不等于所有平台真实账号联调通过，也不等于分发包已经验证。

## 更新 README 截图

```powershell
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe tools/generate_screenshots.py
```

脚本从当前 UI 生成 `image/screenshots/` 中的示例图，包含 Cursor 和高级设置。使用模拟账号、临时数据目录，不访问真实凭据或平台接口，不使用个人背景图。生成后检查文字、裁切和导入导出按钮是否完整可见。

用户提供的实拍截图用于公开文档前，应检查账号标签、额度、余额、代理地址及图片内容；背景图还需要确认可以公开使用。截图中的端口不应照搬为默认配置。

## Windows 构建

先正常退出运行中的打包程序，再执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --name AI额度悬浮窗 --icon favicon.ico --hidden-import keyring.backends.Windows --collect-all charset_normalizer --add-data "config/settings.toml;config" --add-data "faviconV2.png;." --add-data "favicon.ico;." main.py
```

产物目录为 `dist/AI额度悬浮窗/`，需整体分发。只打包显式指定的默认配置和图标；压缩前确认产物目录没有之前试运行留下的 `data/`、日志、凭据或个人图片。不要直接把已长期使用的程序目录压缩上传。

构建后应在干净目录解压、启动，检查无账号首次引导、默认关闭更新、托盘和配置保存，再验证导入导出。当前版本号见 `utils/version.py`。替换默认图标源 `faviconV2.png` 后，可运行 `tools/build_icon.py` 生成多尺寸 `favicon.ico`。

## 发布与来源说明

源码仓库不包含本地审查报告、旧 Gitee 发布脚本、运行数据或构建产物。需要分发安装包时，应从当前源码重新构建并检查产物，再上传发行版附件。

运行时内置更新仍指向 `utils/updater.py` 中的上游 Gitee，默认保持关闭；源码发布到 GitHub 不会自动迁移更新源。

本项目基于原作者的 [AI 额度悬浮窗（wang-guanru/ai-limit-monitoring）](https://gitee.com/wang-guanru/ai-limit-monitoring) 继续开发，感谢原作者的工作。

早期平台额度逻辑曾参考 cc-switch，Cursor 解析参考 CodexBar；这些来源说明应保留。修改适配器时，应以对应平台响应、当前代码及测试为准。仓库目前没有根目录 `LICENSE`，未代替原作者补写授权；再分发前请核对原项目及参考代码的许可并保留适用声明。

数据处理方式见 [安全与隐私说明](../SECURITY.md)。
