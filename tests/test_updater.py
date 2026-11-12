"""
updater 纯函数单元测试(pytest)
================================

【怎么跑】
  .venv\\Scripts\\python.exe -m pytest tests\\test_updater.py -q

【风格说明】
  tests/ 里原有的 test_dock_layout.py 是"离屏渲染 + 手动看效果"的验证脚本;
  本文件是标准 pytest:只测纯函数,不碰网络、不碰 GUI、不需要人工判断。

【为什么只测纯函数】
  latest_release/download_to 这类要真网络,launch_updater 要真起进程,
  放进单测会让 CI 变得又慢又脆。它们的行为靠端到端演练覆盖
  (见 pyinstaller.md 的"自动更新机制"一节);版本比较、附件挑选、
  SHA256 解析、ps1 生成这些"纯输入→纯输出"的函数才值得逐断言锁死。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import updater


# ============================================================
# normalize_version / is_newer
# ============================================================
class TestNormalizeVersion:
    def test_v_prefix(self):
        assert updater.normalize_version("v1.0.1") == (1, 0, 1)

    def test_no_prefix(self):
        assert updater.normalize_version("1.2") == (1, 2, 0)

    def test_prerelease_suffix_ignored(self):
        assert updater.normalize_version("v1.0.0-beta") == (1, 0, 0)

    def test_garbage_returns_zero(self):
        # 解析不出返回 (0,0,0):比较逻辑永不抛异常(静默契约的一部分)
        assert updater.normalize_version("latest") == (0, 0, 0)
        assert updater.normalize_version("") == (0, 0, 0)
        assert updater.normalize_version(None) == (0, 0, 0)


class TestIsNewer:
    def test_patch_newer(self):
        assert updater.is_newer("v1.0.1", "1.0.0")

    def test_equal_is_not_newer(self):
        # v 前缀有无、后缀差异都不算新——否则升级完还会再提示
        assert not updater.is_newer("v1.0.0", "1.0.0")
        assert not updater.is_newer("v1.0.0-beta", "1.0.0")

    def test_numeric_not_string_compare(self):
        # 字符串比较会得出 "10" < "9" 的错案,必须按数值比
        assert updater.is_newer("v1.10.0", "v1.9.0")
        assert not updater.is_newer("v1.9.0", "v1.10.0")

    def test_older_is_not_newer(self):
        assert not updater.is_newer("v1.0.0", "v1.0.1")

    def test_garbage_remote_not_newer(self):
        # 接口返回了奇怪 tag 也不该把用户带进更新流程
        assert not updater.is_newer("banana", "1.0.0")


# ============================================================
# pick_asset
# ============================================================
class TestPickAsset:
    def _release(self, names):
        return {
            "tag_name": "v1.0.1",
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}"}
                       for n in names],
        }

    def test_picks_the_matching_zip(self):
        rel = self._release(["AI额度悬浮窗-v1.0.1.zip"])
        a = updater.pick_asset(rel)
        assert a is not None
        assert a["name"] == "AI额度悬浮窗-v1.0.1.zip"

    def test_ignores_source_archive(self):
        # Gitee 自动打的源码 zip 不能被误选(名字不同)
        rel = self._release(["ai-limit-monitoring.zip", "AI额度悬浮窗-v1.0.1.zip"])
        assert updater.pick_asset(rel)["name"] == "AI额度悬浮窗-v1.0.1.zip"

    def test_no_match_returns_none(self):
        assert updater.pick_asset(self._release(["别的什么.zip"])) is None

    def test_duplicate_takes_last(self):
        # 正常流程 release.py 会拦重复上传;这里保证万一发生时行为确定
        rel = self._release(["AI额度悬浮窗-v1.0.1.zip", "AI额度悬浮窗-v1.0.1.zip"])
        a = updater.pick_asset(rel)
        assert a["browser_download_url"] == "https://x/AI额度悬浮窗-v1.0.1.zip"

    def test_empty_assets(self):
        assert updater.pick_asset({"tag_name": "v1.0.1", "assets": []}) is None


# ============================================================
# parse_sha256
# ============================================================
HEX = "a" * 64
HEX2 = "b" * 64


class TestParseSha256:
    def test_last_line(self):
        body = "**v1.0.0 → v1.0.1 更新内容**\n\n- 修了个 bug\n\nSHA256: " + HEX
        assert updater.parse_sha256(body) == HEX

    def test_takes_last_hit(self):
        # 手改过说明、追加了多次时,以最后追加的为准
        body = f"SHA256: {HEX}\n\nSHA256: {HEX2}"
        assert updater.parse_sha256(body) == HEX2

    def test_missing_returns_none(self):
        assert updater.parse_sha256("只有发行说明") is None
        assert updater.parse_sha256("") is None

    def test_uppercase_hex_lowered(self):
        assert updater.parse_sha256("SHA256: " + HEX.upper()) == HEX

    def test_wrong_length_not_matched(self):
        # 63 位不是合法指纹,不能瞎匹配半截
        assert updater.parse_sha256("SHA256: " + "a" * 63) is None


# ============================================================
# update_supported / replace_allowed 门禁
# ============================================================
class TestGates:
    def test_dev_default_off(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delenv("AIQUOTA_DEV_UPDATE", raising=False)
        assert updater.update_supported() is False

    def test_dev_env_escape_hatch(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv("AIQUOTA_DEV_UPDATE", "1")
        assert updater.update_supported() is True

    def test_frozen_on(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delenv("AIQUOTA_DEV_UPDATE", raising=False)
        assert updater.update_supported() is True

    def test_replace_forbidden_in_dev_even_with_env(self, monkeypatch):
        # 硬门禁:开发环境哪怕设了逃生口也不许替换目录(会毁仓库)
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv("AIQUOTA_DEV_UPDATE", "1")
        assert updater.replace_allowed() is False

    def test_replace_allowed_when_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert updater.replace_allowed() is True


# ============================================================
# build_update_ps1:脚本内容的关键断言
# ============================================================
class TestBuildPs1:
    def _text(self):
        return updater.build_update_ps1_text(
            app_dir=r"C:\Apps\AI额度悬浮窗",
            src_dir=r"C:\Apps\AI额度悬浮窗\data\update\staging\AI额度悬浮窗",
            exe_path=r"C:\Apps\AI额度悬浮窗\AI额度悬浮窗.exe",
            proc_name="AI额度悬浮窗",
            parent_pid=12345,
            log_path=r"C:\Apps\AI额度悬浮窗\data\update\update.log",
            staging_dir=r"C:\Apps\AI额度悬浮窗\data\update\staging",
            zip_path=r"C:\Apps\AI额度悬浮窗\data\update\AI额度悬浮窗-v1.0.1.zip")

    def test_mirror_and_protections(self):
        t = self._text()
        assert "/MIR" in t            # 原地镜像替换
        assert "/XD data" in t        # 保护 data(用户数据 + 更新工作区)
        assert "/XF update.ps1" in t  # 脚本自身不被镜像动作删掉

    def test_paths_and_pid_baked_in(self):
        t = self._text()
        assert r"AI额度悬浮窗\AI额度悬浮窗.exe" in t  # 新版启动路径
        assert r"data\update\update.log" in t     # 日志路径

    def test_wait_restart_selfdelete(self):
        t = self._text()
        assert "$ParentPid = 12345" in t          # 父 PID 烘焙为字面量
        assert "Get-Process -Id $ParentPid" in t  # 等旧进程退出
        assert "Start-Process" in t               # 拉起新版本
        assert "$MyInvocation.MyCommand.Path" in t  # 自删脚本

    def test_chinese_path_escaped_as_single_quotes(self):
        # 单引号字面量:PS 不插值,中文路径原样保留
        t = self._text()
        assert "'C:\\Apps\\AI额度悬浮窗'" in t

    def test_single_quote_in_path_doubled(self):
        t = updater.build_update_ps1_text(
            app_dir="C:\\App's", src_dir="C:\\staging", exe_path="C:\\App's\\x.exe",
            proc_name="x", parent_pid=1, log_path="C:\\l.log",
            staging_dir="C:\\staging", zip_path="C:\\z.zip")
        assert "'C:\\App''s'" in t   # 路径里的单引号按 PS 规则写成两个
