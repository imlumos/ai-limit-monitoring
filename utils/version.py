"""
版本号单一来源 (version)
=========================

【这个文件做什么】
整个项目只在这一处定义版本号,其他地方一律 import,不各自维护:

  - GitHub Release 使用 VERSION_TAG 作为发行标签
  - utils/updater 启动时拿它和 Gitee 最新 tag 比对,判断"有没有新版本"
  - 更新弹窗      显示"当前 vA → 最新 vB"

【为什么要"单一来源"】
自动更新靠"包内嵌的 APP_VERSION"和"发行版的 tag_name"严格一致才能算对账。
如果这两处各写各的、发了 tag=v1.0.2 但包里还是 1.0.1,那么用户升级到
v1.0.2 之后,程序一比对:"我(1.0.1) < 最新(v1.0.2)",还会永远提示有更新。
发行标签、源码版本和安装包版本应保持一致。

构建和分发方法见 docs/development.md。
"""
# 当前版本号。不带 v 前缀;格式 主.次.修订(语义化版本的简化版)。
# 发新版只需要改这一行。
APP_VERSION = "1.0.0-release"

# 派生的 git tag / 发行版名。Gitee 上 tag 都带 v 前缀(releases/latest 返回
# 的 tag_name 形如 "v1.0.1"),比对和拼附件名都用这个,别单独维护。
VERSION_TAG = "v" + APP_VERSION
