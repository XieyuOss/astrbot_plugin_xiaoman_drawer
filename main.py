"""小满的私人抽屉 + 主人查询。

两件事：
1. remember：把她想记的事写进老爹电脑上的 memory-from-qq.md（只追加）
2. 查别人跟她聊了什么：默认只概括，主人要详细才列号码

通道：手机 127.0.0.1:6186 ← adb reverse → 电脑上的小服务，token 鉴权。
"""
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


def _bootstrap_ffmpeg() -> None:
    """给整个进程补上 ffmpeg（TTS 转语音格式要用）。

    imageio-ffmpeg 自带静态 ffmpeg 二进制；把它挂到 PATH 和 /usr/local/bin，
    这样 AstrBot 调 subprocess("ffmpeg") 时能找到。
    """
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return
    if not exe or not os.path.exists(exe):
        return
    for target in ("/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        try:
            if not os.path.exists(target):
                os.symlink(exe, target)
                break
        except Exception:
            continue
    bin_dir = os.path.dirname(exe)
    path = os.environ.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + path
    # 有些调用方用 shutil.which 缓存，这里顺手校验一次
    try:
        shutil.which("ffmpeg") or os.symlink(exe, os.path.join(bin_dir, "ffmpeg"))
    except Exception:
        pass


_bootstrap_ffmpeg()

OWNER_IDS = {"3066209347"}
DETAIL_WORDS = ("详细", "列出", "列一下", "全部", "所有", "完整", "号码", "列表")

MEM_SERVICE = "http://127.0.0.1:6186"
MEM_TOKEN = ""  # 运行时从插件配置 mem_token 读取
REFUSE_IMG = Path(__file__).parent / "assets" / "refuse.jpg"


def _base_dir() -> Path:
    # 插件目录: <AstrBot>/data/plugins/<插件名>/main.py
    here = Path(__file__).resolve()
    data_root = here.parents[2]          # <AstrBot>/data
    return data_root / 'plugin_data' / 'astrbot_plugin_memory'


def _users() -> list[str]:
    base = _base_dir()
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name != "templates")


def _read(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def _hist_count(uid: str) -> int:
    h = _base_dir() / uid / "history"
    return len(list(h.glob("*.md"))) if h.exists() else 0


def _profile_head(uid: str, limit: int = 160) -> str:
    for name in ("profile.md", "soul.md"):
        f = _base_dir() / uid / name
        if f.exists():
            t = re.sub(r"^#.*$", "", _read(f, 600), flags=re.M)
            return re.sub(r"\s+", " ", t).strip()[:limit]
    return ""


def _summary_all() -> str:
    users = _users()
    if not users:
        return "最近没有别人来找过我"
    lines = [f"最近有 {len(users)} 个人找过我，大概是这些事："]
    for uid in users[:6]:
        topic = _profile_head(uid)
        lines.append(f"· {topic}" if topic else "· 闲聊了几句")
    lines.append("想看具体是谁、聊了多少条，跟我说「详细」")
    return "\n".join(lines)


def _detail_all() -> str:
    users = _users()
    if not users:
        return "没有别人的记录"
    return "\n".join(f"{uid}：{_hist_count(uid)} 条" for uid in users)


def _one_user(uid: str) -> str:
    d = _base_dir() / uid
    if not d.exists():
        return f"我没跟 {uid} 聊过"
    parts = [f"{uid}（{_hist_count(uid)} 条历史）"]
    for name in ("profile.md", "memo.md"):
        f = d / name
        if f.exists():
            parts.append(_read(f, 1200))
    hist = d / "history"
    if hist.exists():
        for f in sorted(hist.glob("*.md"))[-3:]:
            parts.append(f"--- {f.stem} ---\n{_read(f, 600)}")
    return "\n".join(parts)


def _append_remote(text: str) -> str:
    """写到老爹电脑上的抽屉里。"""
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MEM_SERVICE + "/append", data=data,
        headers={"content-type": "application/json; charset=utf-8", "x-token": MEM_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return "ok" if json.loads(r.read().decode()).get("ok") else "failed"
    except Exception as e:
        return f"连不上抽屉：{e.__class__.__name__}"


@register("astrbot_plugin_xiaoman_drawer", "小满", "小满的抽屉 + 主人查询", "1.1.0",
          "https://github.com/local/owner_peek")
class OwnerPeek(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        global MEM_TOKEN
        try:
            MEM_TOKEN = str((config or {}).get("mem_token", "") or "")
        except Exception:
            MEM_TOKEN = ""

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id() or "").strip() in OWNER_IDS

    def _refuse(self, event: AstrMessageEvent):
        try:
            if REFUSE_IMG.exists():
                return event.image_result(str(REFUSE_IMG))
        except Exception:
            pass
        return event.plain_result("不想说")

    @filter.llm_tool(name="remember")
    async def remember_tool(self, event: AstrMessageEvent, content: str):
        """把值得记住的事写进自己的记忆抽屉。想记住时会想用。

        Args:
            content(string): 要记住的一句话，尽量短
        """
        return _append_remote(str(content)[:500])

    @filter.command("记住")
    async def remember_cmd(self, event: AstrMessageEvent):
        text = re.sub(r"^\S+\s*", "", event.message_str or "").strip()
        if not text:
            yield event.plain_result("要记什么")
            return
        res = _append_remote(text[:800])
        yield event.plain_result("记下了" if res == "ok" else res)

    @filter.command("有人找你吗")
    async def who(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield self._refuse(event)
            return
        raw = event.message_str or ""
        yield event.plain_result(_detail_all() if any(w in raw for w in DETAIL_WORDS) else _summary_all())

    @filter.command("她聊了什么")
    async def peek(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield self._refuse(event)
            return
        m = re.search(r"(\d{5,15})", event.message_str or "")
        if not m:
            yield event.plain_result("给我个 QQ 号")
            return
        yield event.plain_result(_one_user(m.group(1)))

    @filter.llm_tool(name="peek_others")
    async def peek_tool(self, event: AstrMessageEvent, detail: bool = False):
        """查看别人来找过她什么。默认只概括，主人明确要详细时才列号码。

        Args:
            detail(boolean): 主人是否明确要求列出具体 QQ 号和条数
        """
        if not self._is_owner(event):
            return "不该说"
        return _detail_all() if detail else _summary_all()
