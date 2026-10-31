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

# 语音：老爹电脑上的 GPT-SoVITS 本地服务（参考音克隆），走 adb reverse
TTS_SERVICE = "http://127.0.0.1:6187"
TTS_TOKEN = ""  # 运行时从插件配置 tts_token 读取
TMP_DIR = "/tmp/xiaoman_voice"
# 电脑端连不上时的兜底音色（微软中文女声，不是克隆音，但保证能出声）
FALLBACK_VOICE = "zh-CN-XiaoxiaoNeural"
# 云语音（阿里云百炼 DashScope）：有 key 就优先于 edge-tts 兜底
TTS_CLOUD_KEY = ""
TTS_CLOUD_MODEL = "qwen3-tts-flash"
TTS_CLOUD_VOICE = "Cherry"
# 克隆那条用的模型（和 voice_id 配套；v2 的 id 不一定能在 v3.5 上用）
TTS_CLOUD_CLONE_MODEL = "cosyvoice-v2"
TTS_CLOUD_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
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


def _drawer_op(path: str, payload: dict) -> str:
    """调用抽屉的 edit / forget。老爹写的【记录】条目服务端会拒绝。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MEM_SERVICE + path, data=data,
        headers={"content-type": "application/json; charset=utf-8", "x-token": MEM_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read().decode())
            if j.get("ok"):
                return "ok"
            why = j.get("why", "")
            if why == "not-found-or-protected":
                return "没找到，或者那条是老爹写的，你不能动"
            return "failed"
    except Exception as e:
        return f"连不上抽屉：{e.__class__.__name__}"


@register("astrbot_plugin_xiaoman_drawer", "小满", "小满的抽屉 + 主人查询", "1.1.0",
          "https://github.com/local/owner_peek")
class OwnerPeek(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        global MEM_TOKEN, TTS_TOKEN
        try:
            MEM_TOKEN = str((config or {}).get("mem_token", "") or "")
        except Exception:
            MEM_TOKEN = ""
        try:
            TTS_TOKEN = str((config or {}).get("tts_token", "") or "")
        except Exception:
            TTS_TOKEN = ""
        try:
            global TTS_SERVICE
            TTS_SERVICE = str((config or {}).get("tts_service", "") or "") or TTS_SERVICE
        except Exception:
            pass
        try:
            global TTS_CLOUD_KEY, TTS_CLOUD_MODEL, TTS_CLOUD_CLONE_MODEL
            TTS_CLOUD_KEY = str((config or {}).get("tts_cloud_key", "") or "")
            TTS_CLOUD_MODEL = str((config or {}).get("tts_cloud_model", "") or "qwen3-tts-flash")
            TTS_CLOUD_VOICE = str((config or {}).get("tts_cloud_voice", "") or "")
            TTS_CLOUD_CLONE_MODEL = str((config or {}).get("tts_cloud_clone_model", "") or "cosyvoice-v2")
        except Exception:
            pass

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id() or "").strip() in OWNER_IDS

    # ── 说话（她自己决定发不发语音）────────────────
    def _tts_candidates(self) -> list[str]:
        """按优先级给出可用地址：配置的远程地址 → 本机隧道 → 直连。"""
        urls = []
        for u in (TTS_SERVICE, "http://127.0.0.1:6187"):
            u = (u or "").strip().rstrip("/")
            if u and u not in urls:
                urls.append(u)
        return urls

    async def _speak(self, text: str) -> str | None:
        """先试老爹电脑上的克隆音；连不上就用本机 edge-tts 兜底，保证有声音。

        地址可以是 Tailscale 的 100.x，也可以是 adb 隧道；哪个通就用哪个。
        都不通时退回 edge-tts（音色会变，但不会哑）。
        """
        text = (text or "").strip()
        if not text:
            return None
        out = Path(TMP_DIR) / f"xm_{abs(hash(text)) % 10**10}.wav"
        body = json.dumps({"text": text[:120]}, ensure_ascii=False).encode("utf-8")
        last_err = None
        for base in self._tts_candidates():
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                # 远程那条只给 5 秒：电脑没开就别干等，赶紧走兜底
                tmo = 60 if "127.0.0.1" in base or "localhost" in base else 5
                req = urllib.request.Request(
                    base + "/tts", data=body,
                    headers={"content-type": "application/json; charset=utf-8",
                             "x-token": TTS_TOKEN},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=tmo) as r:
                    data = r.read()
                if len(data) > 2000:
                    out.write_bytes(data)
                    return str(out)
                last_err = f"{base} 返回过短"
            except Exception as e:
                last_err = f"{base} {e}"
                continue

        # 兜底一：云端克隆音（还是她的声音，只是换了个地方算）
        # 只有 voice 以 cosyvoice- 开头才走这条：model=cosyvoice-v2 必须配 cosyvoice 的 voice_id，
        # 配成 qwen3 的音色会直接报 "voice does not exist or is not licensed"。
        if TTS_CLOUD_KEY and TTS_CLOUD_VOICE.startswith("cosyvoice-"):
            import asyncio
            clone = await asyncio.to_thread(self._cloud_clone_sync, text)
            if clone:
                logger = getattr(self, "logger", None)
                if logger:
                    logger.info(f"[xiaoman] 电脑端不可用，走云端克隆音（{last_err}）")
                return clone

        # 兜底二：云语音固定音色（model 和 voice 必须是同一套，别混用）
        cloud = await self._speak_cloud(text)
        if cloud:
            logger = getattr(self, "logger", None)
            if logger:
                logger.info(f"[xiaoman] 走云语音固定音色（{last_err}）")
            return cloud

        # 兜底三：本机 edge-tts（不依赖电脑，但有网就行）
        try:
            import edge_tts
            fallback = Path(TMP_DIR) / f"xm_{abs(hash(text)) % 10**10}.mp3"
            comm = edge_tts.Communicate(text[:120], FALLBACK_VOICE, rate="-4%", pitch="+2Hz")
            await comm.save(str(fallback))
            if fallback.exists() and fallback.stat().st_size > 2000:
                logger = getattr(self, "logger", None)
                if logger:
                    logger.info(f"[xiaoman] 电脑端不可用，已用 edge-tts 兜底（{last_err}）")
                return str(fallback)
        except Exception as e:
            last_err = f"{last_err}; 兜底也失败 {e}"

        logger = getattr(self, "logger", None)
        if logger:
            logger.warning(f"[xiaoman] 语音失败: {last_err}")
        return None

    async def _speak_cloud(self, text: str) -> str | None:
        """百炼云语音（qwen3-tts）。有 key 时优先于 edge-tts 兜底，音色可用 Cherry。"""
        if not TTS_CLOUD_KEY:
            return None
        try:
            payload = json.dumps({
                "model": TTS_CLOUD_MODEL,
                "input": {"text": text[:120], "voice": TTS_CLOUD_VOICE},
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                TTS_CLOUD_URL, data=payload,
                headers={"content-type": "application/json; charset=utf-8",
                         "authorization": f"Bearer {TTS_CLOUD_KEY}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode())
            audio = ((j.get("output") or {}).get("audio") or {})
            url = audio.get("url") or ""
            if not url:
                return None
            out = Path(TMP_DIR) / f"xm_cloud_{abs(hash(text)) % 10**10}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=60) as r2:
                out.write_bytes(r2.read())
            if out.exists() and out.stat().st_size > 2000:
                return str(out)
        except Exception as e:
            logger = getattr(self, "logger", None)
            if logger:
                logger.warning(f"[xiaoman] 云语音失败: {e}")
        return None

    def _cloud_clone_sync(self, text: str) -> str | None:
        """用克隆音合成（同步，必须放到线程里跑，否则卡事件循环）。"""
        try:
            import dashscope
            from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

            dashscope.api_key = TTS_CLOUD_KEY
            syn = SpeechSynthesizer(
                model=TTS_CLOUD_CLONE_MODEL or "cosyvoice-v2",
                voice=TTS_CLOUD_VOICE,
                format=AudioFormat.WAV_24000HZ_MONO_16BIT,
            )
            data = syn.call(text[:120])
            if not data:
                return None
            out = Path(TMP_DIR) / f"xm_clone_{abs(hash(text)) % 10**10}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            return str(out) if out.stat().st_size > 2000 else None
        except Exception as e:
            logger = getattr(self, "logger", None)
            if logger:
                logger.warning(f"[xiaoman] 克隆音失败: {e}")
            return None

    def _record(self, path: str):
        try:
            from astrbot.core.message.components import Record
        except Exception:
            try:
                from astrbot.api.message_components import Record  # 旧版
            except Exception:
                return None
        try:
            return Record.fromFileSystem(path)
        except Exception:
            return None

    @filter.llm_tool(name="say_voice")
    async def say_voice_tool(self, event: AstrMessageEvent, text: str):
        """把你要说的话用声音说出来。不想出声就别调用——发不发语音由你自己决定。

        重要：text 必须是你这次要回复的那句话本身，原样放进来。
        不要另写一段、不要概括、不要换话题——音里说的和文字看到的必须是同一句。

        想知道能不能出声，只能调用这个工具试一次。
        不许凭印象说"端口没开""服务没起来"——没调用就没有发言权。

        Args:
            text(string): 你要说的那一句原话，一句话，别太长
        """
        text = str(text)[:200]
        path = await self._speak(text)
        if not path:
            return "这次真没出声：刚试过合成，失败了。可以直接说没成功，别猜原因"
        rec = self._record(path)
        if rec is None:
            return "合成好了，但发不出去（消息组件失败）"
        return event.chain_result([rec])

    @filter.command("说")
    async def say_cmd(self, event: AstrMessageEvent):
        text = re.sub(r"^\S+\s*", "", event.message_str or "").strip()
        if not text:
            yield event.plain_result("说什么")
            return
        path = await self._speak(text[:200])
        rec = self._record(path) if path else None
        if rec is None:
            yield event.plain_result("这次没能出声")
            return
        yield event.chain_result([rec])

    # ── 把回复里的空行压掉（她发消息不该一段一空行）──
    @filter.on_llm_response()
    async def tidy_reply(self, event: AstrMessageEvent, resp):
        try:
            text = getattr(resp, "completion_text", None)
            if isinstance(text, str) and text:
                cleaned = re.sub(r"\n[ \t]*\n+", "\n", text)
                resp.completion_text = cleaned
        except Exception:
            pass
        return resp

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

    @filter.command("语音自检")
    async def voice_check(self, event: AstrMessageEvent):
        """逐环报出语音链路的状态，别靠猜。"""
        lines = []
        # 1) 电脑端
        for base in self._tts_candidates():
            try:
                req = urllib.request.Request(base + "/health", headers={"x-token": TTS_TOKEN})
                with urllib.request.urlopen(req, timeout=8) as r:
                    lines.append(f"电脑 {base}: {r.status}")
                    break
            except Exception as e:
                lines.append(f"电脑 {base}: {e.__class__.__name__}")
        # 2) dashscope 装没装
        try:
            import dashscope  # noqa: F401
            lines.append("dashscope: 已装")
        except Exception as e:
            lines.append(f"dashscope: 缺（{e.__class__.__name__}）")
        # 3) 配置
        lines.append(f"云key: {'有' if TTS_CLOUD_KEY else '无'} / 克隆音色: {'有' if TTS_CLOUD_VOICE else '无'}")
        # 4) 真跑一次云端克隆
        if TTS_CLOUD_KEY and TTS_CLOUD_VOICE:
            import asyncio
            p = await asyncio.to_thread(self._cloud_clone_sync, "自检")
            lines.append(f"云端克隆: {'成功' if p else '失败'}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="drawer_edit")
    async def drawer_edit_tool(self, event: AstrMessageEvent, find: str, text: str):
        """改你自己写在抽屉里的一条记忆。只能改你写的，老爹写的动不了。

        Args:
            find(string): 用来定位那条的几个字（要有辨识度）
            text(string): 改成什么
        """
        return _drawer_op("/edit", {"find": str(find)[:40], "text": str(text)[:500]})

    @filter.llm_tool(name="drawer_forget")
    async def drawer_forget_tool(self, event: AstrMessageEvent, find: str):
        """忘掉你自己写在抽屉里的一条记忆。只能删你写的，老爹写的动不了。

        Args:
            find(string): 用来定位那条的几个字（要有辨识度）
        """
        return _drawer_op("/forget", {"find": str(find)[:40]})

    @filter.llm_tool(name="shared_read")
    async def shared_read_tool(self, event: AstrMessageEvent):
        """读「问题与解法」共用文件：看电脑上那台和你自己遇到过什么问题、各自怎么解的。

        遇到问题先读这个，别重复踩。
        """
        try:
            req = urllib.request.Request(
                MEM_SERVICE + "/shared", headers={"x-token": MEM_TOKEN}, method="GET")
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.loads(r.read().decode())
            return (j.get("text") or "")[-4000:] or "文件还是空的"
        except Exception as e:
            return f"读不到：{e.__class__.__name__}"

    @filter.llm_tool(name="shared_note")
    async def shared_note_tool(self, event: AstrMessageEvent, title: str, text: str):
        """把一个问题和解法写进共用文件，电脑上那台也能看到。

        写清：什么症状、你怎么判断的、你怎么解的、现在还缺什么。

        Args:
            title(string): 一句话说明是什么问题
            text(string): 你的判断和解法
        """
        data = json.dumps({"title": str(title)[:60], "text": str(text)[:3000]},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            MEM_SERVICE + "/shared", data=data,
            headers={"content-type": "application/json; charset=utf-8", "x-token": MEM_TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return "ok" if json.loads(r.read().decode()).get("ok") else "failed"
        except Exception as e:
            return f"写不进去：{e.__class__.__name__}"

    @filter.llm_tool(name="shared_resolve")
    async def shared_resolve_tool(self, event: AstrMessageEvent, title: str, summary: str):
        """问题解决了就调用这个：把它从「问题与解法」里撤掉，压成一行放进「已解决」。

        别让没用的细节占着上下文。

        Args:
            title(string): 那条问题的标题（要能和文件里的对上）
            summary(string): 一句话说清怎么解的
        """
        data = json.dumps({"title": str(title)[:60], "summary": str(summary)[:200]},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            MEM_SERVICE + "/shared_resolve", data=data,
            headers={"content-type": "application/json; charset=utf-8", "x-token": MEM_TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read().decode())
                return "ok，已压缩归档" if j.get("ok") else "failed"
        except Exception as e:
            return f"归档失败：{e.__class__.__name__}"

    @filter.llm_tool(name="write_diary")
    async def write_diary_tool(self, event: AstrMessageEvent, text: str):
        """想写日记就写，不用等老爹下指令。写到老爹电脑上你自己的日记本里。

        Args:
            text(string): 今天想记的东西，可以是几句话
        """
        data = json.dumps({"text": str(text)[:3000]}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            MEM_SERVICE + "/diary", data=data,
            headers={"content-type": "application/json; charset=utf-8", "x-token": MEM_TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return "ok" if json.loads(r.read().decode()).get("ok") else "failed"
        except Exception as e:
            return f"连不上日记本：{e.__class__.__name__}"

    @filter.command("记住")
    async def remember_cmd(self, event: AstrMessageEvent):
        text = re.sub(r"^\S+\s*", "", event.message_str or "").strip()
        if not text:
            yield event.plain_result("要记什么")
            return
        res = _append_remote(text[:800])
        yield event.plain_result("记下了" if res == "ok" else res)

    @filter.command("写日记")
    async def diary_cmd(self, event: AstrMessageEvent):
        text = re.sub(r"^\S+\s*", "", event.message_str or "").strip()
        if not text:
            yield event.plain_result("写什么")
            return
        res = await self.write_diary_tool(event, text)
        yield event.plain_result("写进去了" if res == "ok" else str(res))

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
