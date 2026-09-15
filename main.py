#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件二级缓存：按需唤醒、闲时休眠。

思路（来自共犯与萧影的建议）：
不常用的插件常驻会白占 LLM 工具列表与提示词空间。这里把它们列为"受管插件"，
平时保持关闭（休眠），需要时由模型调用 plugin_cache_load 唤醒，
用完之后 plugin_cache_release 或空闲超时自动关回去。

受管条目两种写法，可混用：
1) managed_plugins：配置页直接勾选插件（值为插件名列表，或 ["*"] 表示全部）
2) managed_plugins_extra：每行一条，插件名 | 一句话描述 | 关键词1,关键词2
例：
    astrbot_plugin_qzone_publish | 发QQ空间说说 | 说说,空间,发动态
说明：选择器只会列出“当前已启用”的插件，休眠中的插件请写在 extra 里。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

SELF_PLUGIN = "astrbot_plugin_plugin_cache"


def split_entries(raw: Any) -> list[tuple[str, str, list[str]]]:
    """把配置里的受管条目统一解析成 [(插件名, 描述, [关键词]), ...]。"""
    items: list[Any] = []
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = [f"{k}|{v}" for k, v in raw.items()]
    elif isinstance(raw, str):
        items = raw.replace("；", ";").replace("\n", ";").split(";")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [raw]

    out: list[tuple[str, str, list[str]]] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            desc = str(item.get("description") or "").strip()
            kws = item.get("keywords") or []
            if isinstance(kws, str):
                kws = kws.replace("，", ",").split(",")
            out.append((name, desc, [str(k).strip() for k in kws if str(k).strip()]))
            continue
        s = str(item).strip().replace("，", ",")
        if not s:
            continue
        parts = [p.strip() for p in s.split("|")]
        name = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        kws = [k for k in (parts[2] if len(parts) > 2 else "").split(",") if k]
        if name:
            out.append((name, desc, kws))
    return [(n, d, k) for n, d, k in out if n]


def as_list(raw: Any) -> list[Any]:
    """把配置值统一成列表（字符串、字典、标量都能吃）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, dict):
        return list(raw.keys())
    return [raw]


def normalize_picked(raw: Any) -> tuple[list[str], bool]:
    """解析选择器字段，返回 (插件名列表, 是否勾选了“全部”)。"""
    names: list[str] = []
    wildcard = False
    for item in as_list(raw):
        if isinstance(item, dict):
            name = str(
                item.get("name") or item.get("plugin_name") or item.get("id") or ""
            ).strip()
        else:
            name = str(item).strip()
        if not name:
            continue
        name = name.replace("，", ",").split("|")[0].strip()
        if not name:
            continue
        if name == "*":
            wildcard = True
            continue
        if name not in names:
            names.append(name)
    return names, wildcard


class PluginCachePlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self._warm: dict[str, float] = {}
        self._busy: set[str] = set()
        self._watchdog: asyncio.Task | None = None

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def terminate(self):
        task, self._watchdog = self._watchdog, None
        if task and not task.done():
            task.cancel()

    async def _watchdog_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                await self._reap_idle()
            except asyncio.CancelledError:
                return
            except Exception as e:  # pragma: no cover
                logger.warning(f"[plugin_cache] 巡检异常: {e}")

    # ---------------- 配置 / 元信息 ----------------
    @property
    def entries(self) -> list[tuple[str, str, list[str]]]:
        """勾选的插件 + 手填的条目，合并成统一清单（描述与关键词可选手填）。"""
        raw_picked = self.config.get("managed_plugins")
        picked, wildcard = normalize_picked(raw_picked)
        if wildcard:
            for n in self._all_plugin_names():
                if n != SELF_PLUGIN and n not in picked:
                    picked.append(n)

        detail: list[tuple[str, str, list[str]]] = []
        # 旧配置里可能还留着 "插件名|描述|关键词" 的写法，一并兼容
        detail += split_entries(
            [
                x
                for x in as_list(raw_picked)
                if isinstance(x, dict) or (isinstance(x, str) and "|" in x)
            ]
        )
        detail += split_entries(self.config.get("managed_plugins_extra") or [])

        info: dict[str, tuple[str, list[str]]] = {}
        for n, d, k in detail:
            old_d, old_k = info.get(n, ("", []))
            info[n] = (old_d or d, old_k or k)

        out: list[tuple[str, str, list[str]]] = []
        for n in picked:
            d, k = info.pop(n, ("", []))
            out.append((n, d, k))
        for n, (d, k) in info.items():
            out.append((n, d, k))
        return out

    def _all_plugin_names(self) -> list[str]:
        """取当前已加载的全部插件名（用于选择器里的 “*” 全选）。"""
        names: list[str] = []
        try:
            for meta in self.context.get_all_stars() or []:
                n = getattr(meta, "name", None)
                if n and str(n) not in names:
                    names.append(str(n))
        except Exception as e:  # pragma: no cover
            logger.warning(f"[plugin_cache] 枚举插件失败: {e}")
        return names

    @property
    def idle_seconds(self) -> float:
        try:
            minutes = float(self.config.get("idle_minutes", 30) or 0)
        except (TypeError, ValueError):
            minutes = 30.0
        return max(0.0, minutes) * 60.0

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        groups = [str(x) for x in (self.config.get("enabled_groups") or [])]
        gid = str(event.get_group_id() or "")
        if groups and gid not in groups:
            return False
        if not gid and not self.config.get("enable_private", True):
            return False
        return True

    def _manager(self):
        return getattr(self.context, "_star_manager", None)

    def _meta(self, name: str):
        """按插件名取回 StarMetadata（星标注册表在禁用后仍保留元信息）。"""
        try:
            return self.context.get_registered_star(name)
        except Exception:
            return None

    def _resolve(self, text: str) -> str | None:
        """把模型给的模糊名字对齐到受管插件名。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        names = [n for n, _, _ in self.entries]
        low = raw.lower().lstrip("/")
        for n in names:
            if n.lower() == low:
                return n
        for n in names:
            if n.lower().endswith(low) or low.endswith(n.lower().lstrip("astrbot_plugin_")):
                return n
        for n in names:
            if low in n.lower():
                return n
        return None

    def _is_active(self, name: str) -> bool:
        meta = self._meta(name)
        return bool(getattr(meta, "activated", False))

    async def _set_active(self, name: str, on: bool) -> tuple[bool, str]:
        mgr = self._manager()
        if mgr is None:
            return False, "插件管理器不可用"
        if name == SELF_PLUGIN:
            return False, "不能操作自身"
        try:
            if on:
                await mgr.turn_on_plugin(name)
            else:
                await mgr.turn_off_plugin(name)
        except Exception as e:
            logger.warning(f"[plugin_cache] {name} {'唤醒' if on else '休眠'}失败: {e}")
            return False, str(e)
        if self._is_active(name) != on:
            return False, "状态未变更"
        return True, ""

    # ---------------- 关键词预热 / 空闲回收 ----------------
    async def _keyword_prewarm(self, event: AstrMessageEvent) -> list[str]:
        msg = (event.message_str or "").strip()
        if not msg:
            return []
        hits: list[str] = []
        for name, _desc, kws in self.entries:
            if name in self._warm or not kws:
                continue
            if any(k in msg for k in kws) and not self._is_active(name):
                ok, _err = await self._set_active(name, True)
                if ok:
                    self._warm[name] = time.time()
                    hits.append(name)
                    logger.info(f"[plugin_cache] 关键词预热 {name}")
        return hits

    async def _reap_idle(self):
        if self.idle_seconds <= 0:
            return
        now = time.time()
        for name, ts in list(self._warm.items()):
            if name in self._busy:
                continue
            if now - ts < self.idle_seconds:
                continue
            if self._is_active(name):
                ok, _err = await self._set_active(name, False)
                if ok:
                    logger.info(f"[plugin_cache] 空闲休眠 {name}")
            self._warm.pop(name, None)

    # ---------------- LLM 工具 ----------------
    @filter.llm_tool(name="plugin_cache_load")
    async def plugin_cache_load(
        self, event: AstrMessageEvent, plugin: str, reason: str = ""
    ) -> str:
        """按需唤醒一个正处于休眠的受管插件（二级缓存）。

        Args:
            plugin(string): 受管插件名，例如 astrbot_plugin_qzone_publish
            reason(string): 简要说明为什么要唤醒它
        """
        if not self._session_allowed(event):
            return "当前会话未开放插件二级缓存。"
        name = self._resolve(plugin)
        if name is None:
            return "没有这个受管插件。" + self._catalog_text()
        if self._meta(name) is None:
            return f"插件 {name} 没有安装或没有加载，无法唤醒。"
        if self._is_active(name):
            self._warm[name] = time.time()
            return f"{name} 已经在运行，可以直接调用它的工具。"
        ok, err = await self._set_active(name, True)
        if not ok:
            return f"{name} 唤醒失败：{err}"
        self._warm[name] = time.time()
        return (
            f"{name} 已唤醒。它的工具要下一轮对话才出现在工具列表里，"
            "这轮先把加载结果告诉自己，下一轮再调用它的工具。"
        )

    @filter.llm_tool(name="plugin_cache_release")
    async def plugin_cache_release(self, event: AstrMessageEvent, plugin: str = "") -> str:
        """用完即关：让一个（或全部）已唤醒的受管插件回到休眠。

        Args:
            plugin(string): 受管插件名；留空表示休眠全部
        """
        if not self._session_allowed(event):
            return "当前会话未开放插件二级缓存。"
        if not plugin.strip():
            done = []
            for n in list(self._warm.keys()):
                if self._is_active(n) and (await self._set_active(n, False))[0]:
                    done.append(n)
                self._warm.pop(n, None)
            return f"已休眠：{', '.join(done)}" if done else "没有正在运行的受管插件。"
        name = self._resolve(plugin)
        if name is None:
            return "没有这个受管插件。"
        if not self._is_active(name):
            self._warm.pop(name, None)
            return f"{name} 本来就在休眠。"
        ok, err = await self._set_active(name, False)
        self._warm.pop(name, None)
        return f"{name} 已休眠。" if ok else f"{name} 休眠失败：{err}"

    # ---------------- 提示词 / 指令 ----------------
    def _catalog_text(self, prewarmed: list[str] | None = None) -> str:
        lines = []
        for name, desc, kws in self.entries:
            if self._is_active(name) or name == SELF_PLUGIN:
                continue
            if self._meta(name) is None:
                continue
            tip = f"- {name}"
            if desc:
                tip += f"：{desc}"
            if kws:
                tip += f"（关键词：{'、'.join(kws)}）"
            lines.append(tip)
        if not lines and not prewarmed:
            return ""
        head = (
            "【插件二级缓存】以下插件当前处于休眠，需要用时先调用 plugin_cache_load "
            "唤醒，再在下一轮调用它的工具；用完后可以 plugin_cache_release 关掉："
        )
        body = "\n".join(lines)
        tail = f"\n本轮已自动唤醒：{', '.join(prewarmed)}" if prewarmed else ""
        return head + ("\n" + body if body else "") + tail

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not self._session_allowed(event):
                return
            prewarmed = await self._keyword_prewarm(event)
            if not self.config.get("inject_catalog", True):
                return
            text = self._catalog_text(prewarmed)
            if not text:
                return
            req.system_prompt = (req.system_prompt or "") + "\n" + text
        except Exception as e:
            logger.warning(f"[plugin_cache] on_llm_request 异常: {e}")

    @filter.command("插件缓存", alias={"缓存状态"})
    async def cache_status(self, event: AstrMessageEvent):
        rows = []
        for name, desc, kws in self.entries:
            state = "运行中" if self._is_active(name) else "休眠"
            if name in self._warm:
                state += "（本次已唤醒）"
            rows.append(f"{name}｜{state}｜{desc or '无描述'}")
        if not rows:
            yield event.plain_result("还没有配置任何受管插件。")
            return
        idle = self.config.get("idle_minutes", 30)
        yield event.plain_result(
            "插件二级缓存\n" + "\n".join(rows) + f"\n空闲休眠：{idle} 分钟"
        )
