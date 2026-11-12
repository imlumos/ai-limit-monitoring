"""
自动更新核心 (updater)
======================

【这个文件做什么】
"检查新版 → 下载 zip → 校验 → 解压 → 生成替换脚本"的全部非 UI 逻辑。
UI 层(ui/update_dialog.py)只负责弹窗和线程调度,不碰这些细节。

【整体流程】
  1. latest_release()     匿名请求 Gitee API,拿最新发行版(tag_name/assets/发行说明)
  2. pick_asset()         在 assets 里找到唯一的 zip 附件
  3. is_newer()           拿发行版 tag 和本地 APP_VERSION 比对
  4. download_to()        流式下载 zip 到 data/update/,带进度回调与取消
  5. verify_download()    SHA256 + 大小校验(SHA256 写在发行说明末尾一行)
  6. extract_zip()        在程序内解压到 data/update/staging/(Python 处理中文名最稳)
  7. write_update_ps1()   生成 update.ps1;launch_updater() 脱离式启动它,
                          程序自己退出;脚本等进程消失后 robocopy /MIR 原地替换
                          目录(/XD data 保护用户数据),再拉起新版 exe

【为什么解压放在程序内、替换放在 PowerShell 脚本】
  - zip 条目名是中文("AI额度悬浮窗/..."),Python zipfile 按 UTF-8 标志位解码,
    万无一失;.bat 的编码、Expand-Archive 的路径长度都各有坑。
  - 替换那一刻程序自己必须先退出(Windows 不允许覆盖运行中的 exe),
    所以"最后的替换动作"只能交给一个外部进程(经典的 updater 模式)。

【静默契约】
  启动时的自动检查,任何失败(断网/超时/接口变了)都只记日志、不打扰用户;
  手动"检查更新"(托盘菜单)才会把失败结果弹出来。
  失败永远不影响程序正常运行——更新是增强,不是命脉。

【分层约定】
  本模块不 import PySide6(纯逻辑,可直接单测),也不 import api/
  (api/ 是额度 provider 的地盘,utils 反向依赖会破坏分层)。
  HTTP 直接用 requests,理由同上:api/_http 的异常语义是为 provider 的
  keep-last-good 设计的,这里只需要"失败 = None / UpdateError"的扁平契约。
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from typing import Callable, Optional

import requests

from utils.paths import get_data_path, get_project_root

logger = logging.getLogger(__name__)

# ---- Gitee 仓库信息(仓库搬迁时改这里) -------------------------------
GITEE_API = "https://gitee.com/api/v5"
OWNER = "wang-guanru"
REPO = "ai-limit-monitoring"

# 与 AI额度悬浮窗.spec 的 --name 一致:发行 zip 的顶层目录、exe 名都由它派生
APP_DIR_NAME = "AI额度悬浮窗"

# 更新工作目录名(在 data/ 下):zip、staging、update.log 都住这里。
# 放 data/ 里的好处:robocopy /XD data 天然把这坨临时文件也保护起来。
UPDATE_DIR_NAME = "update"

# 下载前要求磁盘至少剩这么多(解压还要再花一份空间,粗略给 200MB)
MIN_FREE_BYTES = 200 * 1024 * 1024


class UpdateError(Exception):
    """更新流程中可预期失败(网络断、校验不过、用户取消)。

    message 直接是中文,UI 层拿到就能弹给用户看,不用再翻译。
    """


# ============================================================
# 门禁与版本检查
# ============================================================
def update_supported() -> bool:
    """是否启用更新检查。

    - 打包环境(sys.frozen):正式用户,启用。
    - 开发环境:默认关闭;设环境变量 AIQUOTA_DEV_UPDATE=1 可临时打开,
      用来调试"检查 + 弹窗"路径(和 autostart 的门禁同一个思路)。
      注意"替换目录"这一步另有硬门禁(replace_allowed),开发环境永远不放行。
    """
    return bool(getattr(sys, "frozen", False)) or \
        os.environ.get("AIQUOTA_DEV_UPDATE") == "1"


def replace_allowed() -> bool:
    """是否允许真的替换目录并重启(硬门禁)。

    开发环境下 get_project_root() 是仓库根,robocopy /MIR 一跑就等于
    拿构建产物镜像整个仓库——不可恢复。所以替换动作无条件要求 frozen,
    环境变量也救不了(环境变量只放开"检查 + 弹窗"给你看效果)。
    """
    return bool(getattr(sys, "frozen", False))


def latest_release(timeout: float = 10.0) -> Optional[dict]:
    """查询 Gitee 最新发行版;任何失败返回 None(静默契约)。

    公开仓库的该端点无需 token。返回的 dict 里有 tag_name / body / assets,
    其中 assets 只有 name 和 browser_download_url(Gitee 不给 size 字段,
    进度条总大小改用下载响应的 Content-Length)。
    """
    url = f"{GITEE_API}/repos/{OWNER}/{REPO}/releases/latest"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            logger.info("检查更新:Gitee 返回 %s(忽略)", r.status_code)
            return None
        data = r.json()
        if not isinstance(data, dict) or not data.get("tag_name"):
            logger.info("检查更新:响应里没有 tag_name(忽略)")
            return None
        return data
    except (requests.RequestException, ValueError) as e:
        # 断网/超时/JSON 解析失败都算"没有更新",不打扰用户
        logger.info("检查更新失败(忽略): %s", e)
        return None


# ============================================================
# 纯函数(单元测试目标,不碰网络不碰文件系统)
# ============================================================
def normalize_version(tag: str) -> tuple:
    """把版本字符串规整成可比较的 (主, 次, 修订) 整数三元组。

    'v1.0.1' -> (1, 0, 1)   '1.2' -> (1, 2, 0)
    'v1.0.0-beta' -> (1, 0, 0)   预发布后缀直接忽略
    解析不出来(空串/乱码)返回 (0, 0, 0),保证比较不抛异常。
    """
    s = (tag or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    s = s.split("-", 1)[0]          # 去掉 -beta / -rc.1 等预发布后缀
    nums = []
    for part in s.split("."):
        if not part.isdigit():
            return (0, 0, 0)
        nums.append(int(part))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_newer(remote_tag: str, current_version: str) -> bool:
    """远端 tag 是否比本地版本新。逐位按【数值】比较。

    关键点:v1.10.0 必须大于 v1.9.0——字符串比较会得出 "10" < "9" 的错案,
    所以先 normalize 成整数元组再比。相等(含后缀差异)一律返回 False,
    避免"刚升级完又提示有更新"。
    """
    return normalize_version(remote_tag) > normalize_version(current_version)


def pick_asset(release: dict) -> Optional[dict]:
    """在发行版 assets 里挑出本程序的 zip 附件。

    名字必须是 "<APP_DIR_NAME>-<tag>.zip"(如 AI额度悬浮窗-v1.0.1.zip),
    这样 Gitee 自动打包的源码 zip(*.zip 但名字不同)不会被误选。
    重名(重复上传)时取最后一个并告警——正常流程 release.py 会拦住重复上传。
    """
    tag = release.get("tag_name") or ""
    want = f"{APP_DIR_NAME}-{tag}.zip"
    matches = [a for a in (release.get("assets") or [])
               if isinstance(a, dict) and a.get("name") == want]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning("发行版 %s 有 %d 个同名附件 %s,取最后一个",
                       tag, len(matches), want)
    return matches[-1]


_SHA256_RE = re.compile(r"^SHA256:\s*([0-9a-fA-F]{64})\s*$", re.MULTILINE)


def parse_sha256(body: str) -> Optional[str]:
    """从发行说明里解析 SHA256 行(release.py 发版时追加在末尾)。

    多行命中取最后一个(万一旧发行版手改过说明,以最后一次追加为准)。
    没有(如 v1.0.0 时代还没这个机制)返回 None,调用方跳过校验。
    """
    if not body:
        return None
    hits = _SHA256_RE.findall(body)
    return hits[-1].lower() if hits else None


# ============================================================
# 下载 / 校验 / 解压
# ============================================================
def update_paths() -> dict:
    """集中返回更新流程用到的所有路径(UI 层和测试共用这一份)。

    frozen 模式下 app_dir 就是 exe 所在目录 = 要被替换的目录;
    开发环境下这套路径只在测试里用(替换本身有 replace_allowed 门禁)。
    """
    app_dir = get_project_root()
    exe_path = os.path.abspath(sys.executable)
    update_dir = get_data_path(UPDATE_DIR_NAME)     # <根>/data/update
    return {
        "app_dir": app_dir,
        "exe_path": exe_path,
        "proc_name": os.path.splitext(os.path.basename(exe_path))[0],  # 进程名(无 .exe)
        "update_dir": update_dir,
        "staging_dir": os.path.join(update_dir, "staging"),
        "log_path": os.path.join(update_dir, "update.log"),
        "ps1_path": os.path.join(app_dir, "update.ps1"),
    }


def sha256_file(path: str) -> str:
    """流式计算文件 SHA256(48MB 的包一次性读进内存不划算)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_disk_space(path: str) -> None:
    """下载前粗查磁盘剩余空间,不够直接报错(省得下到一半才失败)。"""
    free = shutil.disk_usage(os.path.splitdrive(os.path.abspath(path))[0] or "C:\\").free
    if free < MIN_FREE_BYTES:
        raise UpdateError(f"磁盘剩余空间不足({free // (1024 * 1024)}MB,需要至少 "
                          f"{MIN_FREE_BYTES // (1024 * 1024)}MB)")


def download_to(url: str, dest: str, timeout=(10, 60),
                progress: Optional[Callable[[int, int], None]] = None,
                cancel_check: Optional[Callable[[], bool]] = None) -> int:
    """流式下载 url 到 dest,返回写入字节数。

    - progress(done, total):total 来自响应头 Content-Length,拿不到就是 0
      (进度条退化为"已下载多少 MB")。每满 1MB 回调一次 + 收尾一次,
      避免每个 256KB 分块都往 GUI 线程发信号。
    - cancel_check():GUI 线程把取消标志置位后,这里尽快抛 UpdateError。
    - 下载链接是 browser_download_url(会 302 到带签名的真实地址),
      requests 自动跟随重定向;注意 Gitee 这条链路【只支持 GET】,
      HEAD 会 404,别用它做预探测。

    Gitee 的附件接口不给 size 字段,所以这里不用外部传入 total。
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last_cb = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancel_check and cancel_check():
                        raise UpdateError("已取消")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress and done - last_cb >= 1024 * 1024:
                        last_cb = done
                        progress(done, total)
    except requests.RequestException as e:
        raise UpdateError(f"下载失败: {e}") from e
    if progress:
        progress(done, total)
    if total and done != total:
        raise UpdateError(f"下载不完整:收到 {done} 字节,应为 {total} 字节")
    return done


def verify_download(zip_path: str, expected_sha256: Optional[str],
                    expected_size: Optional[int]) -> None:
    """校验下载产物:先大小后哈希,不符抛 UpdateError。

    expected_sha256 为 None(旧发行版没有 SHA256 行)时跳过哈希校验,
    记一条日志了事——总不能让 v1.0.0 的存量用户永远升不了级。
    """
    actual_size = os.path.getsize(zip_path)
    if expected_size and actual_size != expected_size:
        raise UpdateError(f"文件大小不符:应为 {expected_size} 字节,实际 {actual_size}")
    if not expected_sha256:
        logger.info("发行说明中没有 SHA256 行(旧版发行?),跳过哈希校验: %s", zip_path)
        return
    actual = sha256_file(zip_path)
    if actual != expected_sha256.lower():
        raise UpdateError("SHA256 校验失败,下载文件可能损坏,请稍后重试")
    logger.info("SHA256 校验通过: %s", zip_path)


def extract_zip(zip_path: str, staging_dir: str,
                cancel_check: Optional[Callable[[], bool]] = None) -> str:
    """解压 zip 到 staging_dir,校验唯一顶层目录后返回其完整路径。

    - 先删旧 staging:上次更新的残留混进这次的新目录就说不清了。
    - 逐条检查条目路径:zip 炸弹式绝对路径/C:盘符/../ 上跳一律拒绝。
      (zipfile 本身不写绝对路径,这里是双保险。)
    - 顶层必须是且只能是一个 "AI额度悬浮窗/" 目录(和 release.py 的打包
      结构对齐);结构对不上说明 zip 不是本程序的安装包,直接报错。
    """
    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if cancel_check and cancel_check():
                    raise UpdateError("已取消")
                parts = info.filename.replace("\\", "/").split("/")
                if info.filename.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                    raise UpdateError(f"压缩包内含非法路径: {info.filename}")
            zf.extractall(staging_dir)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"压缩包损坏,无法解压: {e}") from e
    tops = set(os.listdir(staging_dir))
    if tops != {APP_DIR_NAME}:
        raise UpdateError(f"压缩包结构和预期不符(顶层: {sorted(tops)}),"
                          "可能不是本程序的安装包")
    extracted = os.path.join(staging_dir, APP_DIR_NAME)
    if not os.path.isdir(extracted):
        raise UpdateError("解压结果缺少顶层目录,更新中止")
    return extracted


def cleanup_leftovers() -> None:
    """启动时清理上次更新可能留下的半成品(staging 目录 + 已下载 zip)。

    best-effort:清不掉(比如杀软锁着)只记 warning,绝不影响启动。
    update.log 保留——它是排查更新失败唯一的现场。
    """
    p = update_paths()
    try:
        if os.path.isdir(p["staging_dir"]):
            shutil.rmtree(p["staging_dir"])
        if os.path.isdir(p["update_dir"]):
            for name in os.listdir(p["update_dir"]):
                if name.endswith(".zip"):
                    os.remove(os.path.join(p["update_dir"], name))
    except OSError as e:
        logger.warning("清理更新残留失败(忽略): %s", e)


# ============================================================
# update.ps1:替换目录 + 重启(唯一发生在程序体外的一步)
# ============================================================
def _ps_sq(s: str) -> str:
    """转成 PowerShell 单引号字面量(单引号内不插值,路径原样保留;
    内容里的单引号按 PS 规则写成两个)。"""
    return "'" + str(s).replace("'", "''") + "'"


def build_update_ps1_text(app_dir: str, src_dir: str, exe_path: str, proc_name: str,
                          parent_pid: int, log_path: str,
                          staging_dir: str, zip_path: str) -> str:
    """生成 update.ps1 的全文(纯函数,方便单测断言内容)。

    所有变量(路径/PID)直接"烘焙"成脚本里的字面量,不用参数传递——
    免去引号转义和编码问题的最后一处风险面。

    脚本步骤(详见模块头注释):
      1. 等待旧进程退出(按 PID + 按进程名双保险,60 秒超时则放弃且不动文件)
      2. robocopy /MIR 原地镜像;/XD data 保护用户数据;/XF 脚本自身
      3. 清理 staging 和 zip(失败只记日志)
      4. 启动新版本,自删脚本
    """
    return f"""# =====================================================================
# AI额度悬浮窗 自动更新脚本(由程序生成,请勿手工编辑)
# 作用:等旧进程退出后,用 staging 里的新版目录镜像替换安装目录,
#       然后重新启动程序。全程写日志到 {log_path}
# =====================================================================
$ErrorActionPreference = 'Continue'

$AppDir    = {_ps_sq(app_dir)}
$SrcDir    = {_ps_sq(src_dir)}
$ExePath   = {_ps_sq(exe_path)}
$ProcName  = {_ps_sq(proc_name)}
$ParentPid = {int(parent_pid)}
$LogFile   = {_ps_sq(log_path)}
$StagingDir = {_ps_sq(staging_dir)}
$ZipPath   = {_ps_sq(zip_path)}

function Log([string]$msg) {{
    Add-Content -Path $LogFile -Encoding UTF8 -Value ("[{{0}}] {{1}}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
}}

Log "=== 开始更新:旧进程 PID=$ParentPid ==="

# ---- 1) 等待旧进程退出(最多 60 秒,每 0.5 秒查一次) ----
# 按 PID 等主进程;按进程名再兜一层——用户如果双开了第二个实例,
# 它也锁着目录里的文件,必须等同名进程全部退出再动手。
$deadline = (Get-Date).AddSeconds(60)
while ($true) {{
    $pidAlive  = [bool](Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)
    $nameAlive = [bool](Get-Process -Name $ProcName -ErrorAction SilentlyContinue)
    if (-not $pidAlive -and -not $nameAlive) {{ break }}
    if ((Get-Date) -gt $deadline) {{
        Log "等待旧进程退出超时,放弃本次更新(未修改任何文件)"
        exit 1
    }}
    Start-Sleep -Milliseconds 500
}}
Log "旧进程已全部退出"

# ---- 2) 原地镜像替换 ----
# /MIR     目标目录与源完全一致(多的删、旧的覆盖、新的拷入)
# /XD data 排除 data 目录:账号/配置/日志/更新工作区全在里面,一个字节都不能动
#          (robocopy 对排除项"既不拷贝也不删除",正好当保护名单用)
# /XF update.ps1 脚本自身不在镜像范围,否则第一步就把自己删了
# /R:2 /W:2 失败重试 2 次、间隔 2 秒(应对杀软短暂锁文件)
robocopy $SrcDir $AppDir /MIR /XD data /XF update.ps1 /R:2 /W:2 /NFL /NDL /NP
$rc = $LASTEXITCODE
if ($rc -ge 8) {{
    Log "robocopy 失败,退出码 $rc(staging 已保留,可重试或人工排查)"
    exit 2
}}
Log "目录替换完成(robocopy 退出码 $rc)"

# ---- 3) 清理下载物(staging + zip);失败不影响更新结果 ----
try {{ Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction Stop }} catch {{ Log "清理 staging 失败: $_" }}
if (Test-Path -LiteralPath $ZipPath) {{
    try {{ Remove-Item -LiteralPath $ZipPath -Force -ErrorAction Stop }} catch {{ Log "清理 zip 失败: $_" }}
}}
Log "清理完成"

# ---- 4) 启动新版本,自删脚本 ----
Start-Process -FilePath $ExePath -WorkingDirectory $AppDir
Log "已启动新版本,更新完成"
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force
exit 0
"""


def write_update_ps1(src_dir: str, parent_pid: int) -> str:
    """按当前程序的实际路径生成 update.ps1 并写盘,返回脚本路径。

    encoding='utf-8-sig'(带 BOM):Windows PowerShell 5.1 没有 BOM 会把
    中文路径按 ANSI 码页解码,目录名就废了。BOM 是它的"身份说明书"。
    """
    p = update_paths()
    text = build_update_ps1_text(
        app_dir=p["app_dir"], src_dir=src_dir, exe_path=p["exe_path"],
        proc_name=p["proc_name"], parent_pid=parent_pid,
        log_path=p["log_path"], staging_dir=p["staging_dir"],
        zip_path=_known_zip_path(p["update_dir"]))
    with open(p["ps1_path"], "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    return p["ps1_path"]


def _known_zip_path(update_dir: str) -> str:
    """找 update 目录里唯一的 zip(下载产物);找不到就退回占位名。

    正常流程一定有一个;这个兜底只是让 ps1 的清理段不至于拿空路径。
    """
    if os.path.isdir(update_dir):
        zips = [n for n in os.listdir(update_dir) if n.endswith(".zip")]
        if zips:
            return os.path.join(update_dir, zips[0])
    return os.path.join(update_dir, f"{APP_DIR_NAME}-update.zip")


def launch_updater(ps1_path: str) -> bool:
    """脱离式启动 update.ps1;失败记日志返回 False(程序继续正常运行)。

    【为什么是 CREATE_NO_WINDOW 而不是 DETACHED_PROCESS】(v1.0.2 事故修复)
      实测某 Win10 19044 环境:DETACHED_PROCESS 启动的 powershell 会被
      静默吞掉——Popen 返回成功、进程却从未执行脚本(疑似安全软件拦截),
      表现为"用户点了更新、程序退出、然后什么都没发生"。
      CREATE_NO_WINDOW 同样不创建可见窗口,且实测稳定执行,故改用之。
    - CREATE_NEW_PROCESS_GROUP:独立进程组,不受父进程组的 Ctrl+C 等信号影响。
    - -ExecutionPolicy Bypass:绕过"禁止运行未签名脚本"的系统策略
      (只对本次 -File 调用生效,不改系统设置)。

    【启动确认】脚本第一行就会写 update.log;这里等 1.5 秒确认日志真的
    出现,没出现就返回 False——调用方据此【不退出程序】并提示用户,
      避免再次出现"更新器根本没跑,程序却已经退了"的静默失败。
    """
    log_path = update_paths()["log_path"]
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", ps1_path]
    try:
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True)
    except OSError as e:
        logger.error("启动更新脚本失败: %s", e)
        return False
    # 等脚本"落地"的信号:update.log 出现(脚本开头就写第一行日志)。
    # 1.5 秒覆盖 powershell 冷启动;超时视为被拦截/秒死,宁可放弃更新。
    for _ in range(15):
        time.sleep(0.1)
        if os.path.exists(log_path):
            logger.info("更新脚本已启动并确认运行: %s", ps1_path)
            return True
    logger.error("更新脚本启动后未确认运行(update.log 未出现),放弃更新: %s", ps1_path)
    return False
