"""梗百科：遇到不懂的网络梗时，先查本地全量字典，再查 cnmeme.wiki 公开 API。

设计要点：
- 本地字典：`index.json` 由抓取数据生成（标题→slug），命中后直接读 `details/<slug>.json`，
  约 2ms 返回；未命中才走在线 API，并把详情落盘，下次本地命中。
- 只读公开接口，无需 Key；带 6 小时内存缓存。
- 返回内容把「怎么用/怎么接」放最前，控制在 1200 字内。
"""

import asyncio
import html as htmllib
import json
import re
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from quart import jsonify, request

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

API_BASE = "https://cnmeme.wiki/api/v1/public/memes"
PLUGIN_DIR = Path(__file__).parent
BUNDLED_DB_PATH = PLUGIN_DIR / "data" / "memes.db"   # 随插件分发的只读快照
DEFAULT_DATA_DIR = PLUGIN_DIR / "data"               # 兜底（拿不到 plugin_data 时）
SYNC_SCRIPT = PLUGIN_DIR / "scripts" / "sync_memes.py"
CACHE_TTL = 6 * 3600
CACHE_MAX = 300
MAX_CHARS = 1200
SEARCH_CLIP = 900           # 联网搜索结果注入上限（字符）
TIMEOUT = 12
FAST_TIMEOUT = 1.5          # 预查时的总超时，避免拖慢回复
UA = "AstrBot-memewiki/1.0"

# 渲染用字体：按顺序找第一个能用的（Linux Noto → Windows/macOS 常见字体）
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG
    candidates = candidates + _FONT_CANDIDATES_REG
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


@register(
    "astrbot_plugin_memewiki",
    "local",
    "遇到不懂的网络梗时查本地梗字典与 cnmeme.wiki",
    "1.5.2",
)
class MemeWiki(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = dict(config or {})
        # 用户数据与运行时缓存统一放 data/plugin_data/<插件名>/（AstrBot 规范位置），
        # 不写在插件目录里，避免升级/重装丢数据。
        self._data_dir = self._resolve_data_dir()
        self._index_path = self._data_dir / "index.json"
        self._detail_dir = self._data_dir / "details"
        self._db_path = self._data_dir / "memes.db"
        self._bundled_db_path = BUNDLED_DB_PATH
        self._terms_path = self._data_dir / "user_terms.json"
        self._state_path = self._data_dir / "settings.json"
        self._state: dict = {}
        self._load_state()
        self._migrate_legacy_data()
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, str]] = {}
        self._slang = self._load_slang()
        self._titles: dict[str, str] = {}
        self._load_local_index()
        self._user_terms: dict[str, dict] = self._load_user_terms()
        self._terms_lock = asyncio.Lock()
        self._register_web_apis()
        self._sync_task: asyncio.Task | None = None
        self._last_sync_day: str = ""

    def _whale_allows(self, event) -> bool:
        return str(event.unified_msg_origin) in set(self.config.get("enabled_sessions", []))

    # ---------------- WebUI 扩展页面接口 ----------------

    def _register_web_apis(self) -> None:
        routes = [
            ("/astrbot_plugin_memewiki/terms", self._api_terms_list, ["GET"], "列出自定义黑话"),
            ("/astrbot_plugin_memewiki/terms/save", self._api_terms_save, ["POST"], "新增或更新自定义黑话"),
            ("/astrbot_plugin_memewiki/terms/delete", self._api_terms_delete, ["POST"], "删除自定义黑话"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 注册接口 {route} 失败: {exc}")
        logger.info("[memewiki] 已注册 WebUI 接口：terms / terms/save / terms/delete")

    @staticmethod
    def _term_row(name: str, entry: dict) -> dict:
        return {
            "name": name,
            "meaning": str(entry.get("meaning") or ""),
            "author": str(entry.get("author") or ""),
            "author_name": str(entry.get("author_name") or ""),
            "ts": str(entry.get("ts") or ""),
        }

    async def _api_terms_list(self):
        items = [
            self._term_row(name, entry)
            for name, entry in sorted(
                self._user_terms.items(),
                key=lambda kv: str(kv[1].get("ts") or ""),
                reverse=True,
            )
        ]
        return jsonify({"status": "ok", "count": len(items), "items": items})

    async def _api_terms_save(self):
        data = (await request.get_json(silent=True)) or {}
        name = str(data.get("name") or "").strip()
        meaning = str(data.get("meaning") or "").strip()
        if not (1 < len(name) <= 16):
            return jsonify({"status": "error", "message": "名称要 2–16 个字"}), 400
        if not meaning or len(meaning) > 300:
            return jsonify({"status": "error", "message": "含义要 1–300 个字"}), 400

        async with self._terms_lock:
            existed = name in self._user_terms
            self._user_terms[name] = {
                "meaning": meaning,
                "author": "webui",
                "author_name": "WebUI",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_user_terms()
        self._invalidate_term_cache(name)
        logger.info(f"[memewiki] WebUI {'更新' if existed else '新增'}黑话: {name}")
        return jsonify({"status": "ok", "existed": existed, "count": len(self._user_terms)})

    async def _api_terms_delete(self):
        data = (await request.get_json(silent=True)) or {}
        name = str(data.get("name") or "").strip()
        async with self._terms_lock:
            if name not in self._user_terms:
                return jsonify({"status": "error", "message": f"没有「{name}」这条黑话"}), 404
            self._user_terms.pop(name, None)
            self._save_user_terms()
        self._invalidate_term_cache(name)
        logger.info(f"[memewiki] WebUI 删除黑话: {name}")
        return jsonify({"status": "ok", "count": len(self._user_terms)})

    # ---------------- 每天自动同步 ----------------

    async def initialize(self):
        # 与 _maybe_sync() 及 schema 默认值保持一致：默认关闭
        if not self.config.get("auto_sync_enable", False):
            logger.info("[memewiki] 自动同步已关闭（auto_sync_enable=false）")
            return
        hour = int(self.config.get("auto_sync_hour", 4)) % 24
        logger.info(f"[memewiki] 自动同步已启用：每天 {hour:02d}:00")
        self._sync_task = asyncio.create_task(self._auto_sync_loop())

    async def _auto_sync_loop(self):
        while True:
            try:
                await self._maybe_sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 自动同步检查异常: {exc}")
            await asyncio.sleep(1500)   # 每 25 分钟检查一次

    async def _maybe_sync(self):
        if not self.config.get("auto_sync_enable", False):
            return
        hour = int(self.config.get("auto_sync_hour", 4)) % 24
        now = time.localtime()
        if now.tm_hour != hour:
            return
        today = time.strftime("%Y-%m-%d")
        if self._last_sync_day == today:
            return
        if not SYNC_SCRIPT.exists():
            logger.warning(f"[memewiki] 同步脚本不存在: {SYNC_SCRIPT}")
            return

        self._last_sync_day = today
        logger.info(f"[memewiki] 开始同步词库 → {self._data_dir}")
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(SYNC_SCRIPT), "--data-dir", str(self._data_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            tail = (out or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
            logger.info(f"[memewiki] 同步结束 rc={proc.returncode} 用时 {int(time.time()-started)}s | " + " / ".join(tail))
            self._reload_db()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 同步失败: {exc}")

    # ---------------- 基础设施 ----------------

    def _load_slang(self) -> dict[str, str]:
        """可选的群内词表：放在插件数据目录里（不放插件包），没有就是空字典。"""
        path = self._data_dir / "group_slang.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                logger.info(f"[memewiki] 载入本地群梗 {len(data)} 条")
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[memewiki] 载入群梗失败: {exc}")
        return {}

    # ---------------- 自定义黑话字典（与 wiki 字典分开） ----------------

    def _resolve_data_dir(self) -> Path:
        """用户数据目录：data/plugin_data/astrbot_plugin_memewiki/（AstrBot 规范位置）。"""
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir("astrbot_plugin_memewiki"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 取 plugin_data 目录失败（{exc}），回退到插件目录 data/")
            return DEFAULT_DATA_DIR

    def _migrate_legacy_data(self) -> None:
        """把旧版本写在插件目录里的数据搬到 plugin_data，避免用户升级后丢黑话。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 创建数据目录失败: {exc}")
            return

        # 1) 黑话
        legacy_terms = PLUGIN_DIR / "user_terms.json"
        if legacy_terms.exists() and not self._terms_path.exists():
            try:
                self._terms_path.write_text(legacy_terms.read_text(encoding="utf-8"), encoding="utf-8")
                legacy_terms.rename(legacy_terms.with_name("user_terms.json.migrated"))
                logger.info(f"[memewiki] 已把黑话迁移到 {self._terms_path}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 迁移黑话失败: {exc}")

        # 2) 旧的抓取缓存（index.json / details/）同样搬过去
        for name in ("index.json", "details"):
            legacy = PLUGIN_DIR / "data" / name
            target = self._data_dir / name
            if legacy.exists() and not target.exists():
                try:
                    if legacy.is_dir():
                        import shutil

                        shutil.copytree(legacy, target)
                    else:
                        target.write_bytes(legacy.read_bytes())
                    logger.info(f"[memewiki] 已迁移旧缓存 {name} → {target}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[memewiki] 迁移 {name} 失败: {exc}")

    def _load_user_terms(self) -> dict[str, dict]:
        try:
            data = json.loads(self._terms_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                logger.info(f"[memewiki] 载入自定义黑话 {len(data)} 条（{self._terms_path}）")
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"[memewiki] 读取自定义黑话失败: {exc}")
        return {}

    def _save_user_terms(self) -> None:
        # 留一份上一版备份，误删/覆盖时能捞回来
        try:
            if self._terms_path.exists():
                backup = self._terms_path.with_name(self._terms_path.name + ".bak")
                backup.write_text(self._terms_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 备份黑话文件失败: {exc}")
        tmp = self._terms_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._user_terms, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._terms_path)

    def _invalidate_term_cache(self, name: str = "") -> None:
        """黑话增删改后清掉相关缓存。

        _miss_cache 是"未命中"缓存（10 分钟），如果不清，刚加的黑话
        在最长 10 分钟内仍然查不到——用户会以为没生效。
        """
        try:
            if name:
                self._cache.pop(name, None)
                self._miss_cache.pop(name, None)
            else:
                self._cache.clear()
                self._miss_cache.clear()
        except Exception:  # noqa: BLE001
            pass

    def _find_user_term(self, query: str) -> tuple[str, dict] | None:
        """自定义黑话只认精确词，或"完整词出现在句子里"。"""
        q = (query or "").strip()
        if not q or not self._user_terms:
            return None
        if q in self._user_terms:
            return q, self._user_terms[q]
        best = None
        for name in self._user_terms:
            if len(name) >= 2 and name in q:
                if best is None or len(name) > len(best):
                    best = name
        if best:
            return best, self._user_terms[best]
        return None

    @staticmethod
    def _sender_identity(event) -> tuple[str, str]:
        """返回 (QQ 号, QQ 昵称)。群名片不算——event.get_sender_name() 优先给群名片。"""
        uid = str(event.get_sender_id() or "")
        nick = ""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        sender = None
        if isinstance(raw, dict):
            sender = raw.get("sender")
        elif raw is not None:
            sender = getattr(raw, "sender", None)
        if isinstance(sender, dict):
            nick = str(sender.get("nickname") or "")
        elif sender is not None:
            nick = str(getattr(sender, "nickname", "") or "")
        if not nick:
            nick = str(event.get_sender_name() or "")
        return uid, nick

    @staticmethod
    def _user_term_data(name: str, entry: dict) -> dict[str, Any]:
        uid = str(entry.get("author") or "")
        nick = str(entry.get("author_name") or "")
        if uid and nick:
            who = f"{uid} / {nick}"
        else:
            who = uid or nick or "群友"
        return {
            "title": name,
            "tags": ["群内黑话"],
            "usage": str(entry.get("meaning") or ""),
            "origin": "",
            "other": "",
            "note": "",
            "extra": [],
            "slug": "",
            "custom": f"群内黑话 · 由 {who} 添加",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        return self._session

    async def terminate(self):
        if self._sync_task:
            self._sync_task.cancel()
        # 词库连接不缓存（见 _db），这里没有需要收尾的句柄
        if self._session and not self._session.closed:
            await self._session.close()

    def _cache_get(self, key: str) -> str | None:
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        return None

    def _cache_put(self, key: str, value: str):
        if len(self._cache) >= CACHE_MAX:
            oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest, None)
        self._cache[key] = (time.time() + CACHE_TTL, value)

    @staticmethod
    def _html_to_text(raw: str) -> str:
        s = re.sub(r"<h2[^>]*>", "\n## ", raw or "", flags=re.I)
        s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
        s = re.sub(r"</p>|<br\s*/?>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = htmllib.unescape(s)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    # ---------------- 本地群梗 ----------------

    def _lookup_slang(self, query: str) -> str | None:
        q = query.strip()
        if not q:
            return None
        if q in self._slang:
            return self._slang[q]
        for term, meaning in self._slang.items():
            if term and (term in q or q in term) and len(term) >= 2:
                return meaning
        return None

    # ---------------- 在线词库 ----------------

    async def _api_get(self, url: str, params: dict[str, Any] | None = None) -> dict | None:
        session = await self._get_session()
        for attempt in range(2):
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status == 429:
                        wait = float(resp.headers.get("Retry-After") or 3)
                        await __import__("asyncio").sleep(min(wait, 5))
                        continue
                    if resp.status == 404:
                        return None
                    logger.warning(f"[memewiki] HTTP {resp.status} {url}")
                    return None
            except Exception as exc:
                logger.warning(f"[memewiki] 请求失败({type(exc).__name__}): {exc}")
                if attempt == 0:
                    await __import__("asyncio").sleep(1)
        return None

    # ---------------- 本地全量词库（SQLite，随插件分发） ----------------

    def _readable_db_path(self):
        """优先 plugin_data 里用户同步过的 db，其次插件自带的只读快照。"""
        if self._db_path.exists():
            return self._db_path
        if self._bundled_db_path.exists():
            return self._bundled_db_path
        return None

    def _db(self):
        """开一个只读连接，**调用方用完必须 close()**。

        故意不缓存连接：Windows 上常驻句柄会锁住 memes.db，而 AstrBot 更新插件是
        「先删旧目录、再解压覆盖」，此时插件还活着——句柄没放就会 WinError 32。
        """
        db_path = self._readable_db_path()
        if db_path is None:
            return None
        try:
            return sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 打开词库失败: {exc}")
            return None

    def _reload_db(self) -> None:
        """同步完换库：连接不缓存，重新指向新文件即可（下次查询会重开）。"""
        self._load_local_index()

    def _load_local_index(self):
        # 优先用 SQLite；没有 db 才退回 index.json + details/
        db_path = self._readable_db_path()
        if db_path is not None:
            conn = self._db()
            if conn is not None:
                try:
                    count = conn.execute("SELECT value FROM meta WHERE key='count'").fetchone()
                    built = conn.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
                    logger.info(f"[memewiki] 词库 {count[0] if count else '?'} 条"
                                f"（生成于 {built[0] if built else '?'}，来自 {db_path}）")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[memewiki] 读取词库信息失败: {exc}")
                finally:
                    conn.close()
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            self._titles = data.get("titles") or {}
            logger.info(f"[memewiki] 本地索引 {len(self._titles)} 条（生成于 {data.get('built_at', '?')}）")
        except FileNotFoundError:
            self._titles = {}
        except Exception as exc:
            logger.warning(f"[memewiki] 读取本地索引失败: {exc}")
            self._titles = {}

    def _local_match(self, query: str) -> str | None:
        """只认完整梗名：消息里必须出现完整标题，反过来（消息是标题前缀）不算。

        例：「你是什么」不能命中「你是什么冠军」；「你说你是什么冠军」可以。
        """
        q = (query or "").strip()
        if not q:
            return None

        conn = self._db()
        if conn is not None:
            try:
                row = conn.execute("SELECT title FROM memes WHERE title = ?", (q,)).fetchone()
                if row:
                    return row[0]
                row = conn.execute(
                    "SELECT title FROM memes WHERE length(title) >= 2 AND instr(?, title) > 0 "
                    "ORDER BY length(title) DESC LIMIT 1",
                    (q,),
                ).fetchone()
                return row[0] if row else None
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 查词库失败: {exc}")
                return None
            finally:
                conn.close()

        if not self._titles:
            return None
        if q in self._titles:
            return q
        best = None
        for title in self._titles:
            if len(title) >= 2 and title in q:
                if best is None or len(title) > len(best):
                    best = title
        return best

    def _db_row_data(self, row) -> dict[str, Any]:
        title, slug, tags, usage, origin, other, summary = row
        usage = usage or ""
        origin = origin or ""
        if not usage and not origin:
            other = other or summary or ""
        return {
            "title": title,
            "tags": [t for t in (tags or "").split("、") if t],
            "usage": usage,
            "origin": origin,
            "other": other or "",
            "note": "",
            "extra": [],
            "slug": slug or "",
        }

    def _detail_file(self, slug: str) -> Path:
        return self._detail_dir / (urllib.parse.quote(slug, safe="") + ".json")

    def _local_detail(self, slug: str) -> dict | None:
        path = self._detail_file(slug)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("item")
        except Exception:
            return None

    async def _detail_cached(self, slug: str) -> dict | None:
        """先读磁盘；没有再走 API 并落盘。"""
        item = self._local_detail(slug)
        if item:
            return item
        detail = await self._api_get(API_BASE + "/" + urllib.parse.quote(slug, safe=""))
        item = (detail or {}).get("item")
        if item:
            try:
                self._detail_dir.mkdir(parents=True, exist_ok=True)
                self._detail_file(slug).write_text(
                    json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                logger.warning(f"[memewiki] 写详情缓存失败: {exc}")
        return item

    @staticmethod
    def _clip(s: str, n: int) -> str:
        s = (s or "").strip()
        return s[:n] + "…" if len(s) > n else s

    def _entry_data(self, item: dict, q: str, note: str = "",
                    extra_titles: list[str] | None = None) -> dict[str, Any]:
        body = self._html_to_text(item.get("content_html") or "") or (item.get("summary") or "")
        sections = self._split_sections(body)
        usage = sections.get("usage") or ""
        origin = sections.get("origin") or ""
        other = sections.get("other") or ""
        if not usage and not origin:
            other = body
        return {
            "title": item.get("title") or q,
            "tags": [str(t) for t in (item.get("tags") or [])],
            "usage": usage.strip(),
            "origin": origin.strip(),
            "other": other.strip(),
            "note": (note or "").strip(),
            "extra": extra_titles or [],
            "slug": item.get("slug", ""),
        }

    def _entry_text(self, data: dict, for_llm: bool = True) -> str:
        lines = [f"【梗百科】{data['title']}"]
        if data["note"]:
            lines.append(data["note"])
        if data["tags"]:
            lines.append("标签：" + "、".join(data["tags"]))

        if for_llm:
            # 给模型看：用法在前，方便直接接梗
            if data["usage"]:
                head = "【含义】" if data.get("custom") else "【怎么用 / 怎么接】"
                lines.append(head + "\n" + self._clip(data["usage"], 600))
            if data["origin"]:
                lines.append("【出处（仅用于理解，别照搬）】\n" + self._clip(data["origin"], 450))
        else:
            # 给用户看：先来源，后用法
            if data["origin"]:
                lines.append("【来源】\n" + self._clip(data["origin"], 450))
            if data["usage"]:
                head = "【含义】" if data.get("custom") else "【用法】"
                lines.append(head + "\n" + self._clip(data["usage"], 600))

        if data["other"]:
            lines.append(self._clip(data["other"], 400))
        if data["extra"]:
            lines.append("同批检索到的其它相关词条：" + "、".join(data["extra"]))
        lines.append(data.get("custom") or f"cnmeme.wiki/词条/{data['slug']}")
        return "\n".join(lines)

    async def _search_online(self, query: str, strict: bool = False) -> dict | None:
        data = await self._api_get(API_BASE, {"q": query, "limit": 5})
        if not data:
            return None
        items = data.get("items") or []
        if not items:
            return None

        q = query.strip()
        exact = [it for it in items if (it.get("title") or "").strip() == q]
        picked = exact[0] if exact else None
        if picked is None:
            # 同样只认"完整标题出现在查询里"，不做前缀式模糊匹配
            for it in items:
                title = (it.get("title") or "").strip()
                if len(title) >= 2 and title in q:
                    picked = it
                    break

        # 预查模式更严格：只接受标题与查询词有明确包含关系的条目，
        # 否则会把"我真的特别爱你"这种普通短句当成梗，注入无关内容。
        if picked is None and strict:
            return None

        note = ""
        if picked is None:
            picked = items[0]
            note = f"（词库里没有完全同名的条目，最接近的是「{picked.get('title')}」）\n"
        elif not exact:
            note = f"（没有同名条目，这条标题是「{picked.get('title')}」）\n"

        item = await self._detail_cached(picked["slug"]) or picked
        extra = None
        if not exact:
            others = [it.get("title") for it in items if it.get("slug") != picked.get("slug")]
            extra = [t for t in others if t][:3] or None
        return self._entry_data(item, q, note, extra)

    async def _local_lookup(self, query: str, strict: bool = False) -> dict | None:
        """本地词库命中则秒回；命中但库里没正文时才补一次 API。"""
        title = self._local_match(query)
        if not title:
            return None

        conn = self._db()
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT title, slug, tags, usage, origin, other, summary "
                    "FROM memes WHERE title = ?",
                    (title,),
                ).fetchone()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 读词库失败: {exc}")
                row = None
            finally:
                conn.close()
            if row:
                data = self._db_row_data(row)
                if title.strip() != query.strip():
                    data["note"] = f"（没有同名条目，这条标题是「{title}」）\n"
                return data
            return None

        slug = self._titles.get(title)
        if not slug:
            return None
        item = await self._detail_cached(slug)
        if not item:
            return None
        note = "" if title.strip() == query.strip() else f"（没有同名条目，这条标题是「{title}」）\n"
        return self._entry_data(item, query, note)

    async def _find(self, query: str, strict: bool = False) -> dict | None:
        hit = self._find_user_term(query)
        if hit:
            name, entry = hit
            return self._user_term_data(name, entry)
        local = await self._local_lookup(query, strict=strict)
        if local:
            return local
        return await self._search_online(query, strict=strict)

    @staticmethod
    def _split_sections(text: str) -> dict[str, str]:
        """把词条正文按小标题拆开，分出「用法」「出处」「其它」。"""
        parts = re.split(r"\n##\s*", "\n" + (text or ""))
        usage, origin, other = [], [], []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            head, _, rest = part.partition("\n")
            head = head.strip()
            if any(k in head for k in ("用法", "怎么用", "如何用", "例句", "接梗")):
                usage.append(rest.strip() or head)
            elif any(k in head for k in ("来源", "出处", "背景", "起源", "介绍")):
                origin.append(rest.strip() or head)
            else:
                other.append((head + "\n" + rest).strip() if rest else head)
        return {
            "usage": "\n".join(usage).strip(),
            "origin": "\n".join(origin).strip(),
            "other": "\n".join(other).strip(),
        }

    # ---------------- 对外接口 ----------------

    async def lookup(self, query: str, strict: bool = False) -> str:
        query = (query or "").strip()
        if not query:
            return "查询词为空。"

        cached = self._cache_get(query)
        if cached:
            return cached

        slang = self._lookup_slang(query)
        if slang:
            text = f"【群内梗】{query}\n{slang}"
            self._cache_put(query, text)
            return text

        # 自定义黑话优先级最高（用户自己编的，比 wiki 更贴近群）
        hit = self._find_user_term(query)
        if hit:
            name, entry = hit
            uid = str(entry.get("author") or "")
            nick = str(entry.get("author_name") or "")
            who = f"{uid} / {nick}" if (uid and nick) else (uid or nick or "群友")
            text = (f"【群内黑话】{name}\n{entry.get('meaning', '')}\n（由 {who} 定义）")
            self._cache_put(query, text)
            return text

        # 先查本地全量字典（毫秒级），未命中再走在线检索
        data = await self._find(query, strict=strict)
        if data:
            text = self._entry_text(data, for_llm=True)
            self._cache_put(query, text)
            return text

        return f"词库里没有收录「{query}」。这可能是刚出现的新梗、群内自造词，或者拼写不对。"

    @llm_tool(name="search_meme_wiki")
    async def search_meme_wiki(self, event: AstrMessageEvent, query: str) -> str:
        """查询网络梗、流行语、缩写、黑话或群内自造词的含义与出处。

        **只要消息里出现你没有十足把握的网络新词、梗、缩写或黑话**（比如「大水理论」「寿来」
        「飞八分钱」这种），就必须先调用本工具查一下，再决定怎么回。不确定就直接查，
        不要先猜、不要反问用户"这是啥"，也不要说"我漏掉了哪个梗"。
        对方问"这是什么梗""这词啥意思""XX是谁说的"时同样调用。
        查到结果后**优先用它接梗**（照着"怎么用"里的说法/动作回一句），不要当词典条目念；
        只有对方明确问含义或实在接不上时才解释出处。
        只传那个词或短语（建议不超过 8 个字），不要传整句话。
        你百分之百确定的常见词（如"典""草""6"）不用查。
        查不到结果就如实说不知道，不要编造。

        Args:
            query(string): 要查的梗或词语，尽量简短，例如"大水理论""寿来""狗东"。
        """
        if not self._whale_allows(event):
            return "该功能仅在已启用的群友会话中可用。"
        try:
            return await self.lookup(query)
        except Exception as exc:
            logger.error(f"[memewiki] 查询异常: {exc}")
            return "梗词库暂时查不了，先按字面理解或说不知道。"

    @filter.command("梗")
    async def meme_cmd(self, event: AstrMessageEvent, term: str = ""):
        """查梗：/梗 飞八分钱（配置项 reply_mode 决定文字还是图片）"""
        if not self._whale_allows(event):
            return
        term = (term or "").strip()
        if not term:
            # 允许 /梗 名称 带空格：从原始消息里取
            raw = re.sub(r"^/?梗\s*", "", (event.message_str or "").strip()).strip()
            term = raw
        if not term:
            yield event.plain_result("用法：/梗 <词>")
            return

        data = await self._find(term)
        if not data:
            yield event.plain_result(f"词库里没有收录「{term}」。")
            return

        mode = str(self.config.get("reply_mode") or "image").strip().lower()
        if mode.startswith("image") or mode.startswith("图片"):
            try:
                path = await asyncio.to_thread(self._render_card, data)
                yield event.image_result(str(path))
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memewiki] 图片渲染失败，退回文字: {exc}")
        yield event.plain_result(self._entry_text(data, for_llm=False))

    # ---------------- 自定义黑话（独立于 wiki 字典） ----------------

    @filter.command("黑话")
    async def slang_cmd(self, event: AstrMessageEvent):
        """新增/查看自定义黑话：/黑话 名称 含义 | /黑话 列表 | /黑话 删除 名称"""
        if not self._whale_allows(event):
            return
        raw = re.sub(r"^/?黑话\s*", "", (event.message_str or "").strip()).strip()
        if not raw:
            yield event.plain_result(
                "用法：\n"
                "/黑话 <名称> <含义>   记一条群内黑话\n"
                "/黑话 列表             看看都记了啥\n"
                "/黑话 删除 <名称>      删掉一条（管理员）"
            )
            return

        if raw in ("列表", "list", "清单"):
            if not self._user_terms:
                yield event.plain_result("还没人加过黑话。用 /黑话 <名称> <含义> 加一条。")
                return
            items = sorted(self._user_terms.items(), key=lambda kv: kv[1].get("ts", ""), reverse=True)
            lines = [f"共 {len(items)} 条自定义黑话："]
            for name, entry in items[:40]:
                uid = str(entry.get("author") or "")
                nick = str(entry.get("author_name") or "")
                who = f"{uid} / {nick}" if (uid and nick) else (uid or nick or "群友")
                lines.append(f"· {name}：{str(entry.get('meaning'))[:40]}（{who}）")
            if len(items) > 40:
                lines.append(f"…… 还有 {len(items) - 40} 条")
            yield event.plain_result("\n".join(lines))
            return

        if raw.startswith("删除") or raw.startswith("删 "):
            if not event.is_admin():
                yield event.plain_result("删除黑话只有管理员能用。")
                return
            name = re.sub(r"^删除?\s*", "", raw).strip()
            async with self._terms_lock:
                if name not in self._user_terms:
                    yield event.plain_result(f"没有「{name}」这条黑话。")
                    return
                self._user_terms.pop(name, None)
                self._save_user_terms()
            yield event.plain_result(f"已删除「{name}」。")
            return

        parts = raw.split(maxsplit=1)
        name = parts[0].strip().strip("：:，,")
        meaning = parts[1].strip() if len(parts) > 1 else ""
        if not meaning:
            yield event.plain_result("还差含义。格式：/黑话 <名称> <含义>\n例：/黑话 狗东 京东")
            return
        if not (1 < len(name) <= 16):
            yield event.plain_result("黑话名称要 2–16 个字。")
            return
        if len(meaning) > 300:
            yield event.plain_result("含义太长了，控制在 300 字以内。")
            return

        uid, nick = self._sender_identity(event)
        async with self._terms_lock:
            existed = name in self._user_terms
            self._user_terms[name] = {
                "meaning": meaning,
                "author": uid,
                "author_name": nick,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_user_terms()
        self._invalidate_term_cache(name)

        logger.info(f"[memewiki] {'更新' if existed else '新增'}黑话: {name} = {meaning[:40]} by {uid}({nick})")
        yield event.plain_result(
            f"已{'更新' if existed else '记下'}「{name}」：{meaning}\n"
            f"记录人：{uid} / {nick}｜共 {len(self._user_terms)} 条自定义黑话。"
        )

    # ---------------- 词条卡片渲染 ----------------

    def _render_card(self, data: dict) -> Path:
        W, PAD = 900, 44
        font_title = _font(42, bold=True)
        font_sec = _font(26, bold=True)
        font_body = _font(24)
        font_meta = _font(20)
        font_small = _font(18)
        inner = W - PAD * 2

        def wrap(text: str, font, width: int) -> list[str]:
            out = []
            for para in (text or "").split("\n"):
                para = para.strip()
                if not para:
                    continue
                cur = ""
                for ch in para:
                    if font.getlength(cur + ch) <= width:
                        cur += ch
                    else:
                        if cur:
                            out.append(cur)
                        cur = ch
                if cur:
                    out.append(cur)
            return out

        blocks: list[tuple] = []   # (kind, lines, font, color, gap)
        blocks.append(("title", wrap(data["title"], font_title, inner), font_title, (17, 24, 39), 0))
        if data["tags"]:
            blocks.append(("meta", wrap("、".join(data["tags"][:8]), font_meta, inner),
                           font_meta, (107, 114, 128), 12))
        if data["origin"]:
            blocks.append(("sec", ["来源"], font_sec, (37, 99, 235), 26))
            blocks.append(("body", wrap(self._clip(data["origin"], 520), font_body, inner),
                           font_body, (55, 65, 81), 10))
        if data["usage"]:
            sec_title = "含义" if data.get("custom") else "用法"
            blocks.append(("sec", [sec_title], font_sec, (37, 99, 235), 22))
            blocks.append(("body", wrap(self._clip(data["usage"], 700), font_body, inner),
                           font_body, (31, 41, 55), 10))
        if data["note"]:
            blocks.append(("meta", wrap(data["note"], font_meta, inner),
                           font_meta, (146, 64, 14), 14))
        if data["extra"]:
            blocks.append(("meta", wrap("相关：" + "、".join(data["extra"]), font_meta, inner),
                           font_meta, (107, 114, 128), 14))

        footer = data.get("custom") or f"cnmeme.wiki/词条/{data['slug']}"
        heights = []
        total = PAD
        for kind, lines, font, color, gap in blocks:
            lh = int(font.size * 1.5)
            h = gap + lh * len(lines)
            heights.append(h)
            total += h
        total += 22 + int(font_small.size * 1.5) + PAD

        img = Image.new("RGB", (W, total), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 8], fill=(37, 99, 235))

        y = PAD
        for (kind, lines, font, color, gap), h in zip(blocks, heights):
            y += gap
            lh = int(font.size * 1.5)
            for line in lines:
                draw.text((PAD, y), line, font=font, fill=color)
                y += lh
            if kind == "title":
                draw.line([(PAD, y + 10), (W - PAD, y + 10)], fill=(229, 231, 235), width=2)
                y += 12

        draw.line([(PAD, total - PAD - 26), (W - PAD, total - PAD - 26)], fill=(229, 231, 235), width=2)
        draw.text((PAD, total - PAD - 20), footer, font=font_small, fill=(156, 163, 175))

        out_dir = Path(tempfile.gettempdir()) / "memewiki_cards"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"meme_{int(time.time() * 1000)}.png"
        img.save(path)

        # 只保留最近 30 张
        try:
            olds = sorted(out_dir.glob("meme_*.png"))
            for p in olds[:-30]:
                p.unlink(missing_ok=True)
        except Exception:
            pass
        return path

    # ---------------- 确定性兜底：不依赖模型自觉 ----------------

    _ASK_PATTERN = re.compile(
        r"(?:是)?(?:什么梗|啥梗|什么来头|什么来历|什么出处|什么意思|啥意思|怎么用|怎么接|怎么玩|如何用)"
        r"(?:啊|呀|呢|的|啊这是)?[？?。！!~～]*$"
    )
    _BARE_PATTERN = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9·]{2,10}$")
    # 明确在问梗：命中这些词才无条件联网
    _INTENT_WORDS = (
        "什么梗", "啥梗", "梗是啥", "什么来头", "什么来历", "什么出处", "啥意思",
        "什么意思", "黑话", "谐音", "缩写", "怎么来的", "谁说的", "怎么用", "怎么接",
    )
    # 明显是闲聊/问状态的整句，别当梗去查（线上高频误报词）
    _CHAT_PHRASES = {
        "我是谁", "错了", "情绪", "接下一句", "你也学一个", "今天看什么", "到底多少了",
        "搜出来了吗", "来点贴吧评价", "都是同龄人", "你同事", "道德这么高",
        "这是谁的手臂", "比我能串", "宝宝你醒了", "中国人能不能飞", "你现在什么人格",
    }
    # 带这些成分的多半是整句想问句，不是词条
    _SENTENCE_WORDS = re.compile(
        r"怎么样|怎么|多少|为什么|能不能|是不是|什么|了吗|了没|一下|你说|要不要|有没有"
    )
    _STOP_WORDS = {
        "在吗", "在不在", "哈哈", "哈哈哈", "笑死", "好的", "好吧", "晚安", "早安",
        "睡觉", "吃饭", "干嘛", "没事", "收到", "谢谢", "好耶", "草", "典", "乐",
    }
    _WAKE_WORDS = ("小鲸鱼", "大鲸鱼", "大肥鲸", "小肥鲸", "大肥鱼", "奶鱼", "鲸鱼", "deepseek", "ds")
    # 带这些成分的多半是「你/我」开头的整句，不是词条
    _PRONOUN_WORDS = ("自己", "你们", "我们", "他们", "她们", "咱们", "大家", "别人")
    _MISS_TTL = 600
    _HIT_TTL = 600
    _miss_cache: dict[str, float] = {}
    # 搜到过的结果缓存：query -> (过期时间, 搜索结果原文)
    _hit_cache: dict[str, tuple[float, str]] = {}

    def _clean_text(self, text: str) -> str:
        t = (text or "").strip()
        t = re.sub(r"^(@\S+\s*)+", "", t)          # 去掉开头的 @某人
        for w in self._WAKE_WORDS:                  # 去掉唤醒词
            if t.startswith(w):
                t = t[len(w):].strip(" ，,。.！!？?:：")
                break
        return t.strip()

    def _extract_meme_query(self, text: str) -> str | None:
        t = self._clean_text(text)
        if not t:
            return None
        # Image delivery belongs to meme_manager, not a dictionary web search.
        # Explicit questions about the meaning/source of a meme still work.
        if (
            re.search(r"表情|动图", t)
            and re.match(r"^(?:(?:请|麻烦|帮我|给我|能不能|可以|再|先|那|就|只|你)[，,\s]*){0,4}(?:发|来|给|送|补)", t)
            and not any(word in t for word in self._INTENT_WORDS)
        ):
            return None
        m = self._ASK_PATTERN.search(t)
        if m:
            head = t[: m.start()].strip(" ，,。.！!？?")
            head = head.strip("是").strip(" ，,。.！!？?")
            head = re.sub(r"(这个梗|那个梗|这梗|那梗|这个|那个)$", "", head).strip()
            head = re.sub(r"(到底|究竟)$", "", head).strip()
            if head in {"这", "那", "它", "他", "她", "这个", "那个", "你",
                        "这句话", "那句话", "上面", "刚才", "下文", "前面"}:
                return None
            if len(head) <= 2 and head[:1] in {"这", "那", "它", "他", "她"}:
                return None
            if 1 < len(head) <= 12:
                return head
            return None
        if self._BARE_PATTERN.match(t) and t not in self._STOP_WORDS:
            return t
        return None

    def _append_temp(self, req, text: str) -> bool:
        try:
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 注入失败: {exc}")
            return False

    def _guidance_text(self) -> str:
        """内置使用规则：不依赖用户改人设。"""
        custom = str(self.config.get("guidance_text") or "").strip()
        if custom:
            return custom
        return (
            "[梗百科·内置用法，长期有效]\n"
            "1. 看到你没有十足把握的网络新词、梗、缩写、黑话，先调 `search_meme_wiki` 查，别硬猜、别编；\n"
            "2. 查到之后**先接梗、别科普**：照着词条里的【怎么用/怎么接】用一句，像本来就会；\n"
            "3. 只有对方明确问「这是什么梗/啥意思」才解释出处，一两句就够，不要念词条；\n"
            "4. 查不到就说不知道，不要把两个不相干的梗拼一起；\n"
            "5. 说话用短句，别用「首先/其次/希望对你有帮助」这类客服腔。"
        )

    def _miss_hint(self, query: str) -> str:
        """词库没收录时的兜底提示：让模型用 AstrBot 自带的搜索工具去查。"""
        return (
            f"[梗百科] 词库里没有收录「{query}」。\n"
            "如果这个词看起来像网络梗/缩写/黑话，而你也不确定它的意思：\n"
            "1. 先调用你可用的网页搜索工具（例如 web_search_deepseek / web_search_baidu 等）查一下，再决定怎么回；\n"
            "2. 搜不到、或者搜索结果里没有明确说法，就老实说不知道，或者反问一句「这什么梗」；\n"
            "3. **绝对不要硬解释、也不要把两个不相干的梗拼在一起**——装懂比不知道更糟。"
        )

    async def _astrbot_search(self, event: AstrMessageEvent, query: str) -> str:
        """优先用 AstrBot 自带的搜索（用户在 AstrBot 里配好的那个 provider）。"""
        try:
            from types import SimpleNamespace

            from astrbot.core.tools import web_search_tools as wst

            cfg = self.context.get_config(umo=event.unified_msg_origin)
            ps = cfg.get("provider_settings", {}) if cfg else {}
            if not ps.get("web_search"):
                return ""
            provider = str(ps.get("websearch_provider") or "").strip().lower()
            tool_map = {
                "deepseek": "DeepSeekWebSearchTool",
                "baidu_ai_search": "BaiduWebSearchTool",
                "baidu": "BaiduWebSearchTool",
                "tavily": "TavilyWebSearchTool",
                "serpapi": "SerpApiWebSearchTool",
                "brave": "BraveWebSearchTool",
                "bocha": "BochaWebSearchTool",
                "exa": "ExaWebSearchTool",
                "firecrawl": "FirecrawlWebSearchTool",
                "anysearch": "AnySearchWebSearchTool",
            }
            cls_name = tool_map.get(provider)
            if not cls_name or not hasattr(wst, cls_name):
                return ""
            shim = SimpleNamespace(
                context=SimpleNamespace(event=event, context=self.context)
            )
            tool = getattr(wst, cls_name)()
            timeout = float(self.config.get("search_timeout", 6) or 6)
            res = await asyncio.wait_for(tool.call(shim, query=query, count=5, top_k=5), timeout=timeout)
            text = str(res or "").strip()
            if not text or text.startswith("Error"):
                logger.info(f"[memewiki] AstrBot 搜索无结果({provider}): {text[:80]}")
                return ""
            clipped = self._clip(text, SEARCH_CLIP)
            logger.info(
                f"[memewiki] AstrBot 搜索命中({provider}): {query} → 原始 {len(text)} 字，注入 {len(clipped)} 字"
            )
            return clipped
        except asyncio.TimeoutError:
            logger.info(f"[memewiki] AstrBot 搜索超时: {query}")
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] AstrBot 搜索失败({type(exc).__name__}): {exc}")
            return ""

    def _astrbot_search_ready(self, event=None) -> tuple[bool, str]:
        """检查 AstrBot 那边有没有配好搜索，返回 (是否可用, 原因)。"""
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin) if event is not None else self.context.get_config()
            ps = cfg.get("provider_settings", {}) if cfg else {}
            if not ps.get("web_search"):
                return False, "AstrBot 的「网页搜索」总开关是关的"
            provider = str(ps.get("websearch_provider") or "").strip()
            if not provider:
                return False, "AstrBot 没选搜索服务商"
            key_field = {
                "deepseek": "websearch_deepseek_key",
                "baidu_ai_search": "websearch_baidu_app_builder_key",
                "baidu": "websearch_baidu_app_builder_key",
                "tavily": "websearch_tavily_key",
                "serpapi": "websearch_serpapi_key",
                "brave": "websearch_brave_key",
                "bocha": "websearch_bocha_key",
                "exa": "websearch_exa_key",
                "firecrawl": "websearch_firecrawl_key",
                "anysearch": "websearch_anysearch_key",
            }.get(provider)
            if key_field:
                value = ps.get(key_field)
                has_key = bool(value) if isinstance(value, (list, tuple)) else bool(str(value or "").strip())
                if not has_key:
                    return False, f"AstrBot 选的是 {provider}，但没填对应的 API Key"
            return True, provider
        except Exception as exc:  # noqa: BLE001
            return False, f"读取 AstrBot 配置失败: {exc}"

    async def _search_fallback(self, event: AstrMessageEvent, query: str) -> str:
        """只用 AstrBot 已配置的搜索；插件自己不再维护 key。"""
        if not self._allow_plugin_search(self._search_mode()):
            return ""
        text = await self._astrbot_search(event, query)
        if text:
            return text
        return ""

    # ---------------- 联网开关 ----------------
    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                self._state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 读取开关状态失败: {exc}")
            self._state = {}

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 保存开关状态失败: {exc}")

    #: 联网模式取值（配置项 / 网搜 命令 / 状态文件共用）
    #: auto   = 插件代搜，搜不到再提示模型自己搜（默认，最省心）
    #: plugin = 只插件代搜，搜不到就按「没查到」处理，不提示模型
    #: model  = 插件不代搜，只提示模型自己搜
    #: off    = 完全关闭，既不代搜也不提示
    _SEARCH_MODES = ("auto", "plugin", "model", "off")

    def _search_mode(self) -> str:
        """当前联网模式（聊天里 /网搜 设置的优先，其次配置项）。"""
        mode = str(self._state.get("search_mode") or "").strip().lower()
        if mode in self._SEARCH_MODES:
            return mode
        legacy = self._state.get("search_enabled")
        if isinstance(legacy, bool):
            return "plugin" if legacy else "off"
        raw = str(self.config.get("search_mode") or "").strip().lower()
        if raw in self._SEARCH_MODES:
            return raw
        # 1.5.1 之前没有 search_mode 的老配置：用两个布尔推
        if "search_fallback_enabled" in self.config or "miss_hint_enabled" in self.config:
            fallback = bool(self.config.get("search_fallback_enabled", True))
            hint = bool(self.config.get("miss_hint_enabled", True))
            if fallback and hint:
                return "auto"
            if fallback:
                return "plugin"
            return "model" if hint else "off"
        return "auto"

    @staticmethod
    def _allow_plugin_search(mode: str) -> bool:
        """插件自己调 AstrBot 搜索。"""
        return mode in ("auto", "plugin")

    @staticmethod
    def _allow_model_hint(mode: str) -> bool:
        """注入「让模型自己搜」的提示。"""
        return mode in ("auto", "model")

    def _set_search_mode(self, mode: str) -> None:
        self._state["search_mode"] = mode
        self._state.pop("search_enabled", None)
        self._save_state()

    @filter.command("网搜")
    async def cmd_web_search(self, event: AstrMessageEvent):
        """联网模式：/网搜 on|off|model|状态"""
        if not self._whale_allows(event):
            return
        parts = (getattr(event, "message_str", "") or "").strip().split()
        arg = parts[1].lower() if len(parts) > 1 else "状态"
        try:
            max_chars = int(self.config.get("search_max_chars", 10) or 10)
        except (TypeError, ValueError):
            max_chars = 10
        if arg in ("auto", "on", "开", "开启", "1", "自动"):
            self._set_search_mode("auto")
            logger.info("[memewiki] 联网模式 → auto")
            ready, reason = self._astrbot_search_ready(event)
            if ready:
                yield event.plain_result(
                    "联网模式：自动（插件先代搜，搜不到再提示模型自己搜）\n"
                    f"AstrBot 搜索服务商：{reason}\n"
                    f"只在消息 ≤{max_chars} 字、或明确问「什么梗」时才查。"
                )
            else:
                yield event.plain_result(
                    "联网模式：自动，但 AstrBot 那边还没配好搜索，实际只能提示模型自己搜。\n"
                    f"原因：{reason}\n"
                    "要插件代搜：AstrBot 控制台 → 配置 → 网页搜索：开启 + 选服务商 + 填 API Key。"
                )
        elif arg in ("plugin", "插件", "代搜"):
            self._set_search_mode("plugin")
            logger.info("[memewiki] 联网模式 → plugin")
            ready, reason = self._astrbot_search_ready(event)
            if ready:
                yield event.plain_result(
                    f"联网模式：只插件代搜（{reason}）\n"
                    "插件搜不到就按「没查到」处理，不提示模型自己搜。"
                )
            else:
                yield event.plain_result(
                    "已设为只插件代搜，但 AstrBot 那边还没配好搜索，等于完全不联网。\n"
                    f"原因：{reason}"
                )
        elif arg in ("model", "模型", "hint"):
            self._set_search_mode("model")
            logger.info("[memewiki] 联网模式 → model")
            yield event.plain_result(
                "联网模式：让模型自己搜\n"
                "插件不代搜，只在查不到时提示模型用它自己的搜索工具；模型没配搜索就等于不联网。"
            )
        elif arg in ("off", "关", "关闭", "0"):
            self._set_search_mode("off")
            logger.info("[memewiki] 联网模式 → off")
            yield event.plain_result("联网模式：关闭。词库查不到就只说不知道，不联网。")
        else:
            mode = self._search_mode()
            label = {
                "auto": "自动（插件先搜 → 再提示模型）",
                "plugin": "只插件代搜",
                "model": "只提示模型自己搜",
                "off": "关闭",
            }.get(mode, mode)
            ready, reason = self._astrbot_search_ready(event)
            detail = f"可用（{reason}）" if ready else f"不可用：{reason}"
            yield event.plain_result(
                f"当前联网模式：{label}\n"
                f"AstrBot 搜索：{detail}\n"
                f"触发条件：消息 ≤{max_chars} 字，或明确问「什么梗/啥意思」\n"
                "用法：/网搜 auto（推荐）、/网搜 plugin（只代搜）、"
                "/网搜 model（只提醒模型）、/网搜 off"
            )

    def _search_hint(self, query: str, ref: str) -> str:
        return (
            f"[梗百科·网络检索结果·仅供参考]\n"
            f"词库没收录「{query}」，下面是从网上搜到的内容：\n\n{ref}\n\n"
            "要求：\n"
            "1. 用和这个词相关的信息回答，但**不要整段照抄**，用你自己的话说；\n"
            "2. 搜索结果不一定准，如果几种说法互相矛盾、或者明显和梗无关，就说「没查到靠谱的说法」；\n"
            "3. 不要编、不要把两个不相干的梗拼在一起。"
        )

    def _no_search_hint(self, query: str) -> str:
        """消息太长、不触发联网时的轻提示：别硬解释就行。"""
        return (
            f"[梗百科] 词库里没有「{query}」。这条消息太长，没有联网查。\n"
            "如果不确定它是什么意思：别硬解释、别编，最多反问一句「这什么梗」，或者顺着上下文别接这个点。"
        )

    def _remember_hit(self, query: str, ref: str, now: float) -> None:
        """记住这次的搜索结果：10 分钟内同一个词直接复用，不再联网。"""
        if len(self._hit_cache) > 400:
            for k, (exp, _) in list(self._hit_cache.items()):
                if exp <= now:
                    self._hit_cache.pop(k, None)
        self._hit_cache[query] = (now + self._HIT_TTL, ref)

    def _normalize_query(self, query: str) -> str:
        """把「杂鱼杂鱼」「666666」「已取餐已取餐」这类重复折叠成基词，便于命中缓存。"""
        q = (query or "").strip(" ，,。.！!？?、~～")
        n = len(q)
        if n >= 4:
            # 从长到短找重复块：「已取餐已取餐」取 3 字、「666666」取 3 字、「杂鱼杂鱼」取 2 字
            for size in range(min(4, n // 2), 1, -1):
                if n % size == 0 and q == q[:size] * (n // size):
                    return q[:size]
        return q

    def _worth_search(self, raw: str, query: str) -> bool:
        """本地词库没收录时，判断这条查询值不值得联网查。

        线上实测：15:00–16:40 触发 27 次搜索，其中 22 次是「我是谁」「情绪」
        「你对我的好感度怎么样」这类闲聊整句 —— 每次白花 0.4–1.4 秒和 900 字
        注入。这里只放行「明确在问梗」或「像词条的短词」。
        """
        text = (raw or "").strip()
        if any(k in text for k in self._INTENT_WORDS):
            return True
        q = (query or "").strip()
        try:
            max_n = int(self.config.get("search_max_chars", 10) or 10)
        except (TypeError, ValueError):
            max_n = 10
        max_n = max(2, min(max_n, 20))
        if not q or len(q) > max_n:
            return False
        if q in self._CHAT_PHRASES:
            return False
        if self._SENTENCE_WORDS.search(q):
            return False
        if any(k in q for k in self._PRONOUN_WORDS):
            return False
        if len(q) >= 3 and q[:1] in "我你他她它这那":
            return False
        if len(q) >= 4 and q[:1] in "不没别请快都还就再又也":
            return False
        return True

    async def _lookup_fast(self, query: str) -> str:
        """只查本地：群内梗 / 自定义黑话 / 内置词典。毫秒级，不联网。"""
        query = (query or "").strip()
        if not query:
            return ""
        slang = self._lookup_slang(query)
        if slang:
            return f"【群内梗】{query}\n{slang}"
        hit = self._find_user_term(query)
        if hit:
            name, entry = hit
            uid = str(entry.get("author") or "")
            nick = str(entry.get("author_name") or "")
            who = f"{uid} / {nick}" if (uid and nick) else (uid or nick or "群友")
            return f"【群内黑话】{name}\n{entry.get('meaning', '')}\n（由 {who} 定义）"
        data = await self._local_lookup(query, strict=True)
        if data:
            return self._entry_text(data, for_llm=True)
        return ""

    def _dict_hint(self, query: str, text: str) -> str:
        return (
            "[梗词库内部资料·仅用于理解，禁止原样输出]\n"
            f"这是「{query}」的资料。**优先用这个词接梗**：照着【怎么用 / 怎么接】里的说法、"
            "动作或句式，用你自己的口气回一句；对方没明确问含义时，**不要主动解释典故**。"
            "只有在对方明确问「这是什么梗/啥意思」，或者实在接不上时，才用一两句解释出处。"
            "不要复述标题、标签、来源，不要整段照抄，也不要说\"我查了一下\"。\n\n" + text
        )

    @filter.on_llm_request()
    async def prefetch_meme(self, event: AstrMessageEvent, req):
        """消息像梗查询时，先查好注入上下文，避免模型硬猜；同时注入内置用法。"""
        if not self._whale_allows(event):
            return
        try:
            if self.config.get("inject_guidance", True):
                if self._append_temp(req, self._guidance_text()):
                    logger.info("[memewiki] 已注入内置用法说明")
            raw = getattr(event, "message_str", "") or ""
            query = self._extract_meme_query(raw)
            if not query:
                return
            query = self._normalize_query(query)
            mode = self._search_mode()
            now = time.time()

            # ① 先查本地（群内梗 / 黑话 / 内置词典）：毫秒级，命中就注入，
            #    不管联网开关是什么——这些数据本来就在磁盘上，不花钱。
            t_fast = time.time()
            fast_text = await self._lookup_fast(query)
            fast_ms = (time.time() - t_fast) * 1000
            if fast_text:
                self._append_temp(req, self._dict_hint(query, fast_text))
                logger.info(
                    f"[memewiki] 预查命中并注入: {query} ({len(fast_text)} 字, 本地 {fast_ms:.0f}ms)"
                )
                return

            # ② 本地没收录：不像梗就别联网。否则「我是谁」「情绪」「你今天怎么样」
            #    这类闲聊也会跑一次在线词库 + 一次网页搜索，白花 1 秒和 900 字上下文。
            if not self._worth_search(raw, query):
                self._miss_cache[query] = now + self._MISS_TTL
                logger.info(
                    f"[memewiki] 预查未命中且不像梗，跳过联网: {query}（{len(raw)} 字）"
                )
                return

            # ③ 10 分钟内的重复查询：搜到过就复用结果，没搜到就别再联网。
            #    群里连着刷「杂鱼」「杂鱼杂鱼」「666」时，这一段能省掉大半搜索。
            if self._allow_plugin_search(mode):
                hit = self._hit_cache.get(query)
                if hit and hit[0] > now:
                    self._append_temp(req, self._search_hint(query, hit[1]))
                    logger.info(
                        f"[memewiki] 命中搜索缓存: {query}（还剩 {int(hit[0] - now)}s，0ms）"
                    )
                    return
            if self._miss_cache.get(query, 0) > now:
                if mode == "off":
                    self._append_temp(req, self._no_search_hint(query))
                elif self._allow_model_hint(mode):
                    self._append_temp(req, self._miss_hint(query))
                logger.info(
                    f"[memewiki] 10 分钟内查过没结果，跳过重复联网: {query}（模式={mode}）"
                )
                return
            t0 = time.time()
            try:
                text = await asyncio.wait_for(self.lookup(query, strict=True), timeout=FAST_TIMEOUT)
            except asyncio.TimeoutError:
                self._miss_cache[query] = now + self._MISS_TTL
                logger.info(f"[memewiki] 预查超时 {FAST_TIMEOUT}s，跳过: {query}")
                return
            cost_ms = (time.time() - t0) * 1000
            if not text or text.startswith("词库里没有收录"):
                self._miss_cache[query] = now + self._MISS_TTL
                if mode == "off":
                    logger.info(
                        f"[memewiki] 预查未命中: {query}，不联网（模式={mode}，{len(raw)} 字）"
                    )
                    self._append_temp(req, self._no_search_hint(query))
                    return
                if mode == "model":
                    logger.info(f"[memewiki] 预查未命中: {query}，交给模型自己搜")
                    self._append_temp(req, self._miss_hint(query))
                    return
                ref = await self._search_fallback(event, query)
                if ref:
                    self._remember_hit(query, ref, now)
                    logger.info(f"[memewiki] 预查未命中: {query} ({cost_ms:.0f}ms)，搜索兜底命中")
                    self._append_temp(req, self._search_hint(query, ref))
                elif mode == "auto":
                    logger.info(
                        f"[memewiki] 预查未命中: {query} ({cost_ms:.0f}ms)，搜索也没结果，提示模型自己搜"
                    )
                    self._append_temp(req, self._miss_hint(query))
                else:
                    logger.info(
                        f"[memewiki] 预查未命中: {query} ({cost_ms:.0f}ms)，搜索没结果（模式={mode}，不提示）"
                    )
                return
            self._append_temp(req, self._dict_hint(query, text))
            logger.info(f"[memewiki] 预查命中并注入: {query} ({len(text)} 字, {cost_ms:.0f}ms)")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memewiki] 预查异常: {exc}")
