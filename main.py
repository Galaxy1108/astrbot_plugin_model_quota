"""模型自选 + 每日限额插件（金额制）。

功能：
1. ``/model`` 指令：查看开放自选的 AI 大模型、切换当前对话使用的模型。
   私聊谁都可以切；群聊里只有管理员能切换（其他人可查看、可查询额度）。
   哪些模型可被选择由 ``selectable_models`` 白名单决定（留空 = 全部）。
2. ``/quota`` 指令：个人剩余额度渲染成 /ocgo 同款深色图片卡
   （Pillow 绘制；缺 Pillow 或中文字体时回退文本）；管理员可查看全量用量、重置计数。
3. 限额执行：在 ``on_llm_request`` hook 中按「人 × 模型 × 天」累计消费并拦截超限：
   - 每人每天每个模型限额（美元配置）；
   - 每人每天全部模型消费总额度（美元配置）；
   - 部分模型可额外配置全用户每日总限额（美元配置，管理员默认不占用）。
   金额展示一律按汇率换算成人民币。

计费方式：按次计费。每个模型配置一个「单次调用单价（美元）」，
一次唤起 AI 的对话扣一次单价。没有配置单价（0）的模型视为免费，
不受金额限额约束（次数仍会统计展示）。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, ProviderType
from astrbot.api.star import Context, Star

try:
    from astrbot.core.config import AstrBotConfig
except Exception:  # pragma: no cover - 兼容导入
    AstrBotConfig = dict  # type: ignore[assignment,misc]

try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 极旧版本回退为普通字符串
    GreedyStr = str  # type: ignore[assignment,misc]

_USAGE_KEY = "daily_usage_v2"
"""KV 存储中用量数据的键名（v2：金额制）。"""

_UNLIMITED = "不限"
"""无限制时的展示文案。"""

_EPS = 1e-9
"""浮点比较容差。"""

# 进度条字形：与 opencode 用量插件（/ocgo）同款，GBK 安全。
BAR_WIDTH = 10
BAR_FILLED = "█"
BAR_EMPTY = "─"
LIMITED_MARK = "※"

# ---- 额度图片卡（/ocgo 同款深色卡片，Pillow 绘制，无需浏览器） ----
CARD_SCALE = 2
CARD_WIDTH = 620
CARD_PAD = 26
CARD_RADIUS = 16
CARD_BG = (31, 31, 31)
CARD_BORDER = (54, 54, 54)
COLOR_TITLE = (245, 245, 245)
COLOR_LABEL = (232, 232, 232)
COLOR_MUTED = (148, 148, 148)
COLOR_TRACK = (48, 48, 48)
COLOR_GREEN = (63, 185, 80)
COLOR_POOL = (134, 239, 172)
"""总池进度条：浅绿色，与个人条区分。"""
COLOR_AMBER = (210, 153, 34)
COLOR_RED = (248, 81, 73)

FONTS_REGULAR = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
FONTS_BOLD = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/Dengb.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


class ModelQuotaPlugin(Star):
    """模型自选与每日限额插件（金额制）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        cfg = config if isinstance(config, dict) else {}
        raw_selectable = cfg.get("selectable_models", [])
        self.selectable_models: list[str] = (
            [str(x) for x in raw_selectable]
            if isinstance(raw_selectable, list)
            else []
        )
        self._warned_unknown_selectable = False
        raw_names = cfg.get("model_display_names", {})
        self.model_display_names: dict[str, str] = (
            {str(k): str(v) for k, v in raw_names.items()}
            if isinstance(raw_names, dict)
            else {}
        )
        self.default_price: float = self._to_float(
            cfg.get("default_call_price_usd", 0.0), 0.0
        )
        self.model_prices: dict[str, float] = self._to_float_map(
            cfg.get("model_call_prices_usd", {})
        )
        self.default_user_model_quota: float = self._to_float(
            cfg.get("default_user_model_quota_usd", 1.0), 1.0
        )
        self.model_user_quotas: dict[str, float] = self._to_float_map(
            cfg.get("model_user_quotas_usd", {})
        )
        self.default_user_total_quota: float = self._to_float(
            cfg.get("default_user_total_quota_usd", 2.0), 2.0
        )
        self.default_global_quota: float = self._to_float(
            cfg.get("default_global_quota_usd", 0.0), 0.0
        )
        self.model_global_quotas: dict[str, float] = self._to_float_map(
            cfg.get("model_global_quotas_usd", {})
        )
        self.rate: float = self._to_float(cfg.get("usd_to_cny_rate", 7.2), 7.2)
        if self.rate <= 0:
            self.rate = 7.2
        self.admin_exempt: bool = bool(cfg.get("admin_exempt", True))
        self.quota_render: str = (
            str(cfg.get("quota_render", "auto") or "auto").strip().lower()
        )
        if self.quota_render not in ("auto", "image", "text"):
            self.quota_render = "auto"
        self.quota_exceeded_tip: str = str(
            cfg.get(
                "quota_exceeded_tip",
                "⏳ 你今天在 {model} 上的额度已用完（已花 {spent}/{limit}），"
                "明天再来吧～也可以 /model 换个模型试试。",
            )
        )
        self.total_exceeded_tip: str = str(
            cfg.get(
                "total_exceeded_tip",
                "💰 你今天的消费总额度已用完（已花 {spent}/{limit}），明天再来吧～",
            )
        )
        self.global_exhausted_tip: str = str(
            cfg.get(
                "global_exhausted_tip",
                "🈵 {model} 今天的全用户总限额已用完（已花 {spent}/{limit}），"
                "明天再来吧～也可以 /model 换个模型试试。",
            )
        )

    # ------------------------------------------------------------------
    # 配置解析与金额换算
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def _to_float_map(self, value: object) -> dict[str, float]:
        result: dict[str, float] = {}
        if not isinstance(value, dict):
            return result
        for k, v in value.items():
            try:
                f = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                logger.warning(f"model_quota: 配置项 {k!r} 的值 {v!r} 非法，已忽略")
                continue
            if f < 0:
                logger.warning(f"model_quota: 配置项 {k!r} 的值 {v!r} 为负数，已忽略")
                continue
            result[str(k)] = f
        return result

    def price_for(self, provider_id: str) -> float:
        """某模型单次调用单价（美元），0 表示免费。"""
        return self.model_prices.get(provider_id, self.default_price)

    def user_model_limit(self, provider_id: str) -> float:
        """某模型每人每日限额（美元，<=0 不限）。"""
        return self.model_user_quotas.get(provider_id, self.default_user_model_quota)

    def global_limit(self, provider_id: str) -> float:
        """某模型全用户每日总限额（美元，<=0 不限）。"""
        return self.model_global_quotas.get(provider_id, self.default_global_quota)

    def cny(self, usd: float) -> str:
        """美元转人民币展示（¥x.xx）。"""
        return f"¥{usd * self.rate:.2f}"

    @staticmethod
    def _fmt_limit_usd(limit: float, cny_fn) -> str:
        return _UNLIMITED if limit <= 0 else cny_fn(limit)

    # ------------------------------------------------------------------
    # 可选模型白名单 + ocgo 风格展示
    # ------------------------------------------------------------------

    def is_selectable(self, provider_id: str) -> bool:
        """该模型是否开放给用户自选（空列表 = 全部开放）。"""
        return not self.selectable_models or provider_id in self.selectable_models

    @staticmethod
    def _bar(percent: float) -> str:
        """ocgo 同款文本进度条（已用百分比）。"""
        ratio = max(0.0, min(100.0, percent)) / 100.0
        filled = int(round(ratio * BAR_WIDTH))
        if ratio > 0 and filled == 0:
            filled = 1
        return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)

    @staticmethod
    def _reset_line() -> str:
        """每日 00:00 重置的相对 + 绝对时间（ocgo 同款表述）。"""
        now = datetime.now().astimezone()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds = max((tomorrow - now).total_seconds(), 0)
        minutes = max(int(seconds // 60), 1)
        if minutes < 60:
            rel = f"{minutes} 分钟后重置"
        else:
            hours, mins = divmod(minutes, 60)
            rel = f"{hours} 小时 {mins} 分后重置" if mins else f"{hours} 小时后重置"
        return f"⏰ 每日 00:00 重置（{rel}，明天 00:00）"

    # ------------------------------------------------------------------
    # 用量存储：
    # {date, users: {ukey: {name, counts: {pid: n}, spent: {pid: usd}, total: usd}},
    #  global: {pid: usd}, global_counts: {pid: n}}
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().date().isoformat()

    @staticmethod
    def _user_key(event: AstrMessageEvent) -> str:
        """按人标识：平台名 + 发送者 ID（群私聊通用）。"""
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    @staticmethod
    def _num(mapping: dict, key: str) -> float:
        v = mapping.get(key, 0)
        return v if isinstance(v, (int, float)) else 0

    async def _load_usage(self) -> dict:
        data = await self.get_kv_data(_USAGE_KEY, None)
        today = self._today()
        if (
            not isinstance(data, dict)
            or data.get("date") != today
            or not isinstance(data.get("users"), dict)
            or not isinstance(data.get("global"), dict)
        ):
            # 跨天、版本升级或数据异常：整体清零（天然按天重置，无需定时任务）
            data = {"date": today, "users": {}, "global": {}, "global_counts": {}}
            await self.put_kv_data(_USAGE_KEY, data)
        if not isinstance(data.get("global_counts"), dict):
            data["global_counts"] = {}
        return data

    async def _save_usage(self, data: dict) -> None:
        await self.put_kv_data(_USAGE_KEY, data)

    def _user_info(self, data: dict, ukey: str, event: AstrMessageEvent) -> dict:
        info = data["users"].get(ukey)
        if not isinstance(info, dict):
            info = {"name": "", "counts": {}, "spent": {}, "total": 0.0}
            data["users"][ukey] = info
        for k in ("counts", "spent"):
            if not isinstance(info.get(k), dict):
                info[k] = {}
        if not isinstance(info.get("total"), (int, float)):
            info["total"] = 0.0
        try:
            name = event.get_sender_name()
            if name:
                info["name"] = name
        except Exception:
            pass
        return info

    def _recount_global(self, data: dict) -> None:
        """用各用户数据重算全局（删除用户后保持一致）。"""
        total: dict[str, float] = {}
        counts: dict[str, int] = {}
        users = data.get("users", {})
        if isinstance(users, dict):
            for info in users.values():
                if not isinstance(info, dict):
                    continue
                spent = info.get("spent", {})
                if isinstance(spent, dict):
                    for pid, v in spent.items():
                        if isinstance(v, (int, float)) and v > 0:
                            total[pid] = round(total.get(pid, 0.0) + v, 6)
                cnt = info.get("counts", {})
                if isinstance(cnt, dict):
                    for pid, n in cnt.items():
                        if isinstance(n, int) and n > 0:
                            counts[pid] = counts.get(pid, 0) + n
        data["global"] = total
        data["global_counts"] = counts

    # ------------------------------------------------------------------
    # 限额拦截：每次唤起 AI 前触发
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def check_quota(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """超限则直接回复提示并终止本次 LLM 请求；否则扣费后放行。"""
        if event.is_stopped():
            return
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
        except Exception as e:  # 拿不到提供商则放行，不挡正常对话
            logger.warning(f"model_quota: 获取当前模型失败，本次不计数: {e}")
            return
        if provider is None:
            return

        pid = provider.meta().id
        price = self.price_for(pid)
        disp = self._display_name(pid, provider.meta().model or "")

        if self.admin_exempt and event.is_admin():
            return  # 管理员免限额且不计数

        data = await self._load_usage()
        ukey = self._user_key(event)
        info = self._user_info(data, ukey, event)
        counts: dict = info["counts"]
        spent: dict = info["spent"]
        total = info["total"] if isinstance(info["total"], (int, float)) else 0.0

        used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
        spent_m = self._num(spent, pid)
        gspent = self._num(data["global"], pid)

        if price > 0:
            ulimit = self.user_model_limit(pid)
            if ulimit > 0 and spent_m + price > ulimit + _EPS:
                tip = self.quota_exceeded_tip.format(
                    model=disp,
                    used=used_n,
                    spent=self.cny(spent_m),
                    limit=self.cny(ulimit),
                )
                await event.send(event.plain_result(tip))
                event.stop_event()
                return

            tlimit = self.default_user_total_quota
            if tlimit > 0 and total + price > tlimit + _EPS:
                tip = self.total_exceeded_tip.format(
                    spent=self.cny(total), limit=self.cny(tlimit)
                )
                await event.send(event.plain_result(tip))
                event.stop_event()
                return

            glimit = self.global_limit(pid)
            if glimit > 0 and gspent + price > glimit + _EPS:
                tip = self.global_exhausted_tip.format(
                    model=disp,
                    used=data["global_counts"].get(pid, 0),
                    spent=self.cny(gspent),
                    limit=self.cny(glimit),
                )
                await event.send(event.plain_result(tip))
                event.stop_event()
                return

        # 扣费放行（免费模型只计次数）
        counts[pid] = used_n + 1
        data["global_counts"][pid] = (
            data["global_counts"].get(pid, 0) + 1
            if isinstance(data["global_counts"].get(pid), int)
            else 1
        )
        if price > 0:
            spent[pid] = round(spent_m + price, 6)
            info["total"] = round(total + price, 6)
            data["global"][pid] = round(gspent + price, 6)
        await self._save_usage(data)

    # ------------------------------------------------------------------
    # /model 指令（群聊切换仅管理员）
    # ------------------------------------------------------------------

    def _provider_list(self, selectable_only: bool = False) -> list[tuple[str, str]]:
        """返回 [(provider_id, model名)]。

        Args:
            selectable_only: 为 True 时只返回白名单内（开放自选）的模型。
        """
        items: list[tuple[str, str]] = []
        try:
            for p in self.context.get_all_providers():
                meta = p.meta()
                items.append((meta.id, meta.model or ""))
        except Exception as e:
            logger.warning(f"model_quota: 获取模型列表失败: {e}")
        if selectable_only and self.selectable_models:
            if not self._warned_unknown_selectable:
                self._warned_unknown_selectable = True
                known = {pid for pid, _ in items}
                for pid in self.selectable_models:
                    if pid not in known:
                        logger.warning(
                            f"model_quota: selectable_models 中的 {pid!r} "
                            "在当前提供商中不存在，已忽略"
                        )
            items = [it for it in items if it[0] in self.selectable_models]
        return items

    async def _current_provider_id(self, event: AstrMessageEvent) -> str | None:
        try:
            p = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
            return p.meta().id if p else None
        except Exception as e:
            logger.warning(f"model_quota: 获取当前模型失败: {e}")
            return None

    def _find_provider(self, token: str) -> tuple[int, str, str] | None:
        """在开放自选的模型中查找，返回 (序号, id, model)。

        匹配顺序：序号（1 起）> 提供商 ID（忽略大小写）> 显示名（忽略大小写，
        模型名带空格也能匹配，调用方需用 GreedyStr 接参）。
        """
        items = self._provider_list(selectable_only=True)
        token = (token or "").strip()
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(items):
                pid, model = items[idx - 1]
                return idx, pid, model
            return None
        lowered = token.lower()
        for i, (pid, model) in enumerate(items, start=1):
            if pid == token or pid.lower() == lowered:
                return i, pid, model
        for i, (pid, model) in enumerate(items, start=1):
            if self._display_name(pid, model).lower() == lowered:
                return i, pid, model
        return None

    def _remaining_lines(
        self, counts: dict, spent: dict, gspent_map: dict, gcount_map: dict
    ) -> list[str]:
        """个人剩余额度行：ocgo 风格（进度条 + 已用百分比 + 花费 + 重置由调用方统一附）。

        只列开放自选的模型，总池花费并在同一行。
        """
        lines: list[str] = []
        for pid, model in self._provider_list(selectable_only=True):
            price = self.price_for(pid)
            title = self._display_name(pid, model)
            used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
            spent_m = self._num(spent, pid)
            if price <= 0:
                lines.append(f"- {title}：免费·{_UNLIMITED}（已用 {used_n} 次）")
                continue
            ulimit = self.user_model_limit(pid)
            pool_suffix = ""
            glimit = self.global_limit(pid)
            if glimit > 0:
                gspent = self._num(gspent_map, pid)
                pool_suffix = (
                    f" · 总池已花 {self.cny(gspent)}/{self.cny(glimit)}"
                    f"（剩 {self.cny(max(glimit - gspent, 0))}）"
                )
                if gspent + price > glimit + _EPS:
                    pool_suffix += f" {LIMITED_MARK} 总池已用完"
            if ulimit > 0:
                pct = min(spent_m / ulimit * 100.0, 100.0) if ulimit > 0 else 0.0
                exhausted = spent_m + price > ulimit + _EPS
                line = (
                    f"- {title} {self._bar(pct)} {pct:>3.0f}% "
                    f"已花 {self.cny(spent_m)}/{self.cny(ulimit)}"
                    f"（{used_n} 次）剩 {self.cny(max(ulimit - spent_m, 0))}"
                    f"{pool_suffix}"
                )
                if exhausted:
                    line += f"  {LIMITED_MARK} 已用完"
                lines.append(line)
            else:
                lines.append(
                    f"- {title}：已用 {used_n} 次·{self.cny(spent_m)}（个人不限）"
                    f"{pool_suffix}"
                )
        return lines

    def _total_line(self, total: float) -> str:
        """个人消费总额行（ocgo 风格进度条）。"""
        tlimit = self.default_user_total_quota
        if tlimit > 0:
            pct = min(total / tlimit * 100.0, 100.0) if tlimit > 0 else 0.0
            line = (
                f"💰 个人总额 {self._bar(pct)} {pct:>3.0f}% "
                f"已花 {self.cny(total)}/{self.cny(tlimit)}"
                f" 剩 {self.cny(max(tlimit - total, 0))}"
            )
            if total >= tlimit - _EPS:
                line += f"  {LIMITED_MARK} 已用完"
            return line
        return f"💰 个人总额：已花 {self.cny(total)}（总额不限）"

    @staticmethod
    def _user_total(uinfo: dict) -> float:
        total = uinfo.get("total", 0.0) if isinstance(uinfo, dict) else 0.0
        return total if isinstance(total, (int, float)) else 0.0

    # ------------------------------------------------------------------
    # 额度图片卡（深色卡片 + 进度条 + 百分比 + 重置时间）
    # 行结构：(label, percent 个人已用百分比, limited 是否用完,
    #          sub_left, sub_right, pool_percent 总池已用百分比或 None,
    #          pool_sub 总池说明文字或 "")
    # 总池画在同一模型块内的第二条浅绿色进度条，花费与个人行写在同一行。
    # ------------------------------------------------------------------

    def _display_name(self, pid: str, model: str) -> str:
        """展示用真实模型名：显示名映射 > 模型自带名 > ID。

        用户侧展示只用这个，不再出现提供商 ID。
        """
        alias = self.model_display_names.get(pid, "")
        if alias:
            return alias
        if model and model != pid:
            return model
        return pid

    def _quota_card_rows_personal(
        self, counts: dict, spent: dict, gspent_map: dict
    ) -> list[tuple[str, float, bool, str, str, float | None, str]]:
        """组装个人额度卡片行，只含开放自选的模型 + 个人总额行。"""
        rows: list[tuple[str, float, bool, str, str, float | None, str]] = []
        for pid, model in self._provider_list(selectable_only=True):
            price = self.price_for(pid)
            label = self._display_name(pid, model)
            used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
            spent_m = self._num(spent, pid)
            if price <= 0:
                rows.append(
                    (
                        label,
                        0.0,
                        False,
                        f"免费·{_UNLIMITED}（已用 {used_n} 次）",
                        "",
                        None,
                        "",
                    )
                )
                continue
            ulimit = self.user_model_limit(pid)
            glimit = self.global_limit(pid)
            pool_pct: float | None = None
            pool_sub = ""
            if glimit > 0:
                gspent = self._num(gspent_map, pid)
                pool_pct = min(gspent / glimit * 100.0, 100.0)
                pool_sub = (
                    f"总池已花 {self.cny(gspent)}/{self.cny(glimit)}"
                    f"（剩 {self.cny(max(glimit - gspent, 0))}）"
                )
            if ulimit > 0:
                pct = min(spent_m / ulimit * 100.0, 100.0)
                exhausted = spent_m + price > ulimit + _EPS
                sub_left = (
                    f"已花 {self.cny(spent_m)} / 上限 {self.cny(ulimit)}"
                    f"（{used_n} 次）"
                )
                if pool_sub:
                    sub_left += f" · {pool_sub}"
                rows.append(
                    (
                        label,
                        pct,
                        exhausted,
                        sub_left,
                        f"剩 {self.cny(max(ulimit - spent_m, 0))}",
                        pool_pct,
                        "",
                    )
                )
            else:
                sub_left = f"已用 {used_n} 次·{self.cny(spent_m)}（个人不限）"
                if pool_sub:
                    sub_left += f" · {pool_sub}"
                rows.append((label, 0.0, False, sub_left, "", pool_pct, ""))
        return rows

    @staticmethod
    def _first_existing(paths: tuple[str, ...]) -> str | None:
        for path in paths:
            if path and os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _fc_font(pattern: str) -> str | None:
        """用 fontconfig 按 pattern 找字体文件（Linux 优先，路径随发行版而异）。"""
        try:
            import shutil
            import subprocess

            if not shutil.which("fc-match"):
                return None
            out = subprocess.run(
                ["fc-match", pattern, "--format=%{file}\n"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            for line in out.splitlines():
                path = line.strip()
                if (
                    path
                    and os.path.isfile(path)
                    and path.lower().endswith((".ttf", ".ttc", ".otf"))
                ):
                    return path
        except Exception:  # noqa: BLE001
            pass
        return None

    def _font_path(self, bold: bool) -> str | None:
        if bold:
            hit = self._fc_font(":lang=zh:weight=bold") or self._fc_font(
                "Noto Sans CJK SC:weight=bold"
            )
            if hit:
                return hit
        else:
            hit = self._fc_font(":lang=zh") or self._fc_font("Noto Sans CJK SC")
            if hit:
                return hit
        return self._first_existing(FONTS_BOLD if bold else FONTS_REGULAR)

    @staticmethod
    def _fill_color(percent: float, limited: bool) -> tuple[int, int, int]:
        if limited or percent >= 80:
            return COLOR_RED
        if percent >= 50:
            return COLOR_AMBER
        return COLOR_GREEN

    def _card_output_path(self, name: str) -> Path | None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            out_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_model_quota"
            out_dir.mkdir(parents=True, exist_ok=True)
            import hashlib

            digest = hashlib.sha256(name.encode("utf-8", "ignore")).hexdigest()[:8]
            return out_dir / f"quota_{digest}.png"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"model_quota: 卡片输出目录不可用: {exc}")
            return None

    def _render_quota_card(
        self,
        title: str,
        name: str,
        subtitle: str,
        rows: list[tuple[str, float, bool, str, str, float | None, str]],
    ) -> str | None:
        """用 Pillow 绘制 /ocgo 同款额度卡片。返回图片路径，失败返回 None。

        必须跑在 worker 线程（``asyncio.to_thread``），保持同步。
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"model_quota: Pillow 不可用: {exc}")
            return None

        regular = self._font_path(False)
        bold = self._font_path(True) or regular
        if not regular or not bold:
            logger.debug("model_quota: 找不到可用的中文字体，跳过图片渲染")
            return None

        scale = CARD_SCALE
        now = datetime.now().astimezone()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds = max((tomorrow - now).total_seconds(), 0)
        minutes = max(int(seconds // 60), 1)
        if minutes < 60:
            rel = f"{minutes} 分钟后重置"
        else:
            hours, mins = divmod(minutes, 60)
            rel = f"{hours} 小时 {mins} 分后重置" if mins else f"{hours} 小时后重置"
        reset_text = f"每日 00:00 重置 · {rel}"

        def load(path: str, size: int):
            return ImageFont.truetype(path, size * scale)

        f_title = load(bold, 22)
        f_sub = load(regular, 13)
        f_meta = load(regular, 12)
        f_label = load(regular, 16)
        f_pct = load(bold, 16)
        f_small = load(regular, 12)

        width = CARD_WIDTH * scale
        pad = CARD_PAD * scale
        bar_h = 8 * scale
        pool_h = 5 * scale
        gap_label_bar = 9 * scale
        gap_bar_sub = 8 * scale
        gap_bar_pool = 6 * scale
        gap_section = 20 * scale

        probe = ImageDraw.Draw(Image.new("RGB", (width, 8)))

        def line_h(text: str, font) -> int:
            box = probe.textbbox((0, 0), text, font=font)
            return box[3] - box[1]

        title_h = line_h("剩余额度", f_title)
        sub_h = line_h("个人额度 · 已用百分比", f_sub)
        meta_h = line_h("更新于 2000/00/00 00:00:00", f_meta)
        label_h = line_h("总池·provider", f_label)
        subline_h = line_h("已花 ¥000.00 / 上限 ¥000.00", f_small)

        height = pad + title_h + 10 * scale + sub_h + 6 * scale + meta_h + 20 * scale
        for _label, _percent, _limited, _left, _right, _pool_pct, _pool in rows:
            height += label_h + gap_label_bar + bar_h
            if _pool_pct is not None:
                height += gap_bar_pool + pool_h
            height += gap_bar_sub + subline_h
            height += gap_section
        if rows:
            height -= gap_section
        if any(row[2] for row in rows):
            height += 12 * scale + meta_h
        height += pad

        img = Image.new("RGB", (width, height), CARD_BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=CARD_RADIUS * scale,
            fill=CARD_BG,
            outline=CARD_BORDER,
            width=max(1, scale),
        )

        cursor = pad

        # 标题行：左标题，右用户名
        draw.text((pad, cursor), title, font=f_title, fill=COLOR_TITLE)
        name_w = probe.textlength(name, font=f_sub)
        draw.text(
            (width - pad - name_w, cursor + (title_h - sub_h)),
            name,
            font=f_sub,
            fill=COLOR_MUTED,
        )
        cursor += title_h + 10 * scale

        draw.text((pad, cursor), subtitle, font=f_sub, fill=COLOR_MUTED)
        cursor += sub_h + 6 * scale

        draw.text(
            (pad, cursor),
            f"更新于 {now.year}/{now.month}/{now.day} {now:%H:%M:%S} · {reset_text}",
            font=f_meta,
            fill=COLOR_MUTED,
        )
        cursor += meta_h + 20 * scale

        track_w = width - pad * 2
        for label, percent, limited, sub_left, sub_right, pool_pct, _pool in rows:
            color = self._fill_color(percent, limited)
            pct_text = f"{percent:.0f}%"
            pct_w = probe.textlength(pct_text, font=f_pct)
            # 超长标签截断，避免顶到右侧百分比
            max_label_w = track_w - pct_w - 20 * scale
            if probe.textlength(label, font=f_label) > max_label_w:
                trimmed = label
                while len(trimmed) > 1 and probe.textlength(
                    trimmed + "…", font=f_label
                ) > max_label_w:
                    trimmed = trimmed[:-1]
                label = trimmed + "…"
            draw.text((pad, cursor), label, font=f_label, fill=COLOR_LABEL)
            draw.text(
                (width - pad - pct_w, cursor),
                pct_text,
                font=f_pct,
                fill=COLOR_TITLE,
            )
            cursor += label_h + gap_label_bar

            draw.rounded_rectangle(
                (pad, cursor, pad + track_w, cursor + bar_h),
                radius=bar_h // 2,
                fill=COLOR_TRACK,
            )
            if percent > 0:
                fill_w = max(int(track_w * percent / 100.0), bar_h)
                draw.rounded_rectangle(
                    (pad, cursor, pad + fill_w, cursor + bar_h),
                    radius=bar_h // 2,
                    fill=color,
                )
            cursor += bar_h
            # 总池条：同一模型块内的第二条浅绿色细进度条
            if pool_pct is not None:
                cursor += gap_bar_pool
                draw.rounded_rectangle(
                    (pad, cursor, pad + track_w, cursor + pool_h),
                    radius=pool_h // 2,
                    fill=COLOR_TRACK,
                )
                if pool_pct > 0:
                    pool_fill = max(int(track_w * pool_pct / 100.0), pool_h)
                    draw.rounded_rectangle(
                        (pad, cursor, pad + pool_fill, cursor + pool_h),
                        radius=pool_h // 2,
                        fill=COLOR_POOL,
                    )
                cursor += pool_h
            cursor += gap_bar_sub

            draw.text((pad, cursor), sub_left, font=f_small, fill=COLOR_MUTED)
            if sub_right:
                right_w = probe.textlength(sub_right, font=f_small)
                draw.text(
                    (width - pad - right_w, cursor),
                    sub_right,
                    font=f_small,
                    fill=COLOR_MUTED,
                )
            cursor += subline_h + gap_section

        if any(row[2] for row in rows):
            cursor -= gap_section
            draw.text(
                (pad, cursor + 12 * scale),
                f"{LIMITED_MARK} 已用完：等明日 00:00 重置或改用其他模型",
                font=f_meta,
                fill=COLOR_RED,
            )

        out = self._card_output_path(name or "quota")
        if out is None:
            return None
        img.save(out, "PNG")
        return str(out)

    async def _personal_quota_card(
        self,
        event: AstrMessageEvent,
        counts: dict,
        spent: dict,
        gspent_map: dict,
        total: float,
    ) -> str | None:
        """尝试渲染个人额度图片卡；配置为 text 或渲染失败时返回 None（调用方回退文本）。"""
        if self.quota_render == "text":
            return None
        rows = self._quota_card_rows_personal(counts, spent, gspent_map)
        if not rows:
            return None
        # 个人总额行
        tlimit = self.default_user_total_quota
        if tlimit > 0:
            pct = min(total / tlimit * 100.0, 100.0)
            rows.append(
                (
                    "个人总额",
                    pct,
                    total >= tlimit - _EPS,
                    f"已花 {self.cny(total)} / 上限 {self.cny(tlimit)}",
                    f"剩 {self.cny(max(tlimit - total, 0))}",
                    None,
                    "",
                )
            )
        else:
            rows.append(
                ("个人总额", 0.0, False, f"已花 {self.cny(total)}（总额不限）", "", None, "")
            )
        try:
            name = event.get_sender_name() or ""
        except Exception:
            name = ""
        card = await asyncio.to_thread(
            self._render_quota_card,
            "剩余额度",
            name,
            f"个人额度 · 已用百分比（1$≈{self.cny(1)}）",
            rows,
        )
        if card is None and self.quota_render == "image":
            logger.warning("model_quota: 图片渲染失败（缺 Pillow 或中文字体），已回退文本")
        return card

    def _model_help(self) -> str:
        return (
            "🤖 模型自选指令\n"
            "/model —— 查看可选模型、当前模型与剩余额度\n"
            "/model list —— 同上\n"
            "/model use <序号|名称> —— 切换当前对话的模型（例：/model use 2）\n"
            "  快捷写法：/model 2 或 /model Kimi K3（名称带空格也能直接写）\n"
            "/model me —— 只看我今日的剩余额度\n"
            "说明：私聊谁都可以切换；群聊里只有管理员能切换。\n"
            "列表与切换只包含管理员开放的模型（selectable_models）。\n"
            "切换按当前会话生效（私聊按人，群聊按整群），消费按人统计。"
        )

    @filter.command("model", alias={"模型"})
    async def model(
        self,
        event: AstrMessageEvent,
        action: str | None = None,
        target: GreedyStr | None = None,
    ):
        """查看 / 切换当前对话使用的 AI 大模型。"""
        act = (action or "").strip().lower()

        if act in ("", "list", "列表"):
            all_items = self._provider_list()
            if not all_items:
                yield event.plain_result(
                    "当前还没有可用的 AI 模型，请先在 WebUI「模型服务」中配置。"
                )
                return
            items = self._provider_list(selectable_only=True)
            if not items:
                yield event.plain_result(
                    "管理员尚未开放可自选的模型（selectable_models 为空或均不可用）。"
                )
                return
            current = await self._current_provider_id(event)
            data = await self._load_usage()
            ukey = self._user_key(event)
            uinfo = data["users"].get(ukey, {})
            counts = uinfo.get("counts", {}) if isinstance(uinfo, dict) else {}
            spent = uinfo.get("spent", {}) if isinstance(uinfo, dict) else {}
            if not isinstance(counts, dict):
                counts = {}
            if not isinstance(spent, dict):
                spent = {}
            lines = ["🤖 可选 AI 模型（* 为当前对话正在用）："]
            for i, (pid, model) in enumerate(items, start=1):
                mark = " *" if pid == current else ""
                price = self.price_for(pid)
                fee = "免费" if price <= 0 else f"${price:.4f}/次"
                lines.append(f"{i}. {self._display_name(pid, model)} [{fee}]{mark}")
            lines.append("")
            rem_lines = self._remaining_lines(
                counts, spent, data.get("global", {}), data.get("global_counts", {})
            )
            if rem_lines:
                lines.append(f"📊 我今日剩余额度（1$≈{self.cny(1)}）：")
                lines.extend(rem_lines)
                lines.append(self._total_line(self._user_total(uinfo if isinstance(uinfo, dict) else {})))
                lines.append(self._reset_line())
                lines.append("")
            lines.append("切换：/model use <序号|ID>（例：/model use 2）")
            if not event.is_private_chat():
                lines.append("群聊中切换模型仅限管理员。")
            yield event.plain_result("\n".join(lines))
            return

        if act in ("me", "my", "mine", "我的", "额度"):
            data = await self._load_usage()
            ukey = self._user_key(event)
            uinfo = data["users"].get(ukey, {})
            counts = uinfo.get("counts", {}) if isinstance(uinfo, dict) else {}
            spent = uinfo.get("spent", {}) if isinstance(uinfo, dict) else {}
            if not isinstance(counts, dict):
                counts = {}
            if not isinstance(spent, dict):
                spent = {}
            total = self._user_total(uinfo if isinstance(uinfo, dict) else {})
            card = await self._personal_quota_card(
                event, counts, spent, data.get("global", {}), total
            )
            if card:
                yield event.image_result(card)
                return
            lines = [f"📊 我今日剩余额度（1$≈{self.cny(1)}）："]
            rem_lines = self._remaining_lines(
                counts, spent, data.get("global", {}), data.get("global_counts", {})
            )
            lines.extend(rem_lines if rem_lines else ["当前还没有开放可自选的模型。"])
            lines.append(self._total_line(total))
            lines.append(self._reset_line())
            yield event.plain_result("\n".join(lines))
            return

        # 切换：/model use <t> | /model <序号|ID|显示名>
        # 显示名可能带空格（如 GPT 5.6 Luna），target 是 GreedyStr，
        # 会拿到 action 之后的所有剩余文本，这里拼起来再匹配。
        token: str | None = None
        if act in ("use", "用", "切", "换", "qie", "huan"):
            token = str(target or "").strip()
        elif act:
            token = f"{action} {target or ''}".strip()
        if token:
            # 群聊仅管理员可切换
            if not event.is_private_chat() and not event.is_admin():
                yield event.plain_result(
                    "❌ 群聊中切换模型仅限管理员，你可以用 /model 查看模型和剩余额度。"
                )
                return
            found = self._find_provider(token)
            if found is None:
                yield event.plain_result(
                    f"❌ 没找到模型「{token}」，用 /model 查序号后切换（如 /model use 2）。"
                )
                return
            idx, pid, model = found
            disp = self._display_name(pid, model)
            try:
                await self.context.provider_manager.set_provider(
                    provider_id=pid,
                    provider_type=ProviderType.CHAT_COMPLETION,
                    umo=event.unified_msg_origin,
                )
            except Exception as e:
                logger.warning(f"model_quota: 切换模型失败: {e}")
                yield event.plain_result(f"❌ 切换到 {disp} 失败，请稍后再试或联系管理员。")
                return
            # 若目标模型额度已空，给出预警（仍允许切换）
            warn = ""
            price = self.price_for(pid)
            if price > 0 and not (self.admin_exempt and event.is_admin()):
                data = await self._load_usage()
                ukey = self._user_key(event)
                uinfo = data["users"].get(ukey, {})
                spent = uinfo.get("spent", {}) if isinstance(uinfo, dict) else {}
                total = uinfo.get("total", 0.0) if isinstance(uinfo, dict) else 0.0
                total = total if isinstance(total, (int, float)) else 0.0
                spent_m = self._num(spent if isinstance(spent, dict) else {}, pid)
                gspent = self._num(
                    data.get("global", {})
                    if isinstance(data.get("global"), dict)
                    else {},
                    pid,
                )
                ulimit = self.user_model_limit(pid)
                tlimit = self.default_user_total_quota
                glimit = self.global_limit(pid)
                if ulimit > 0 and spent_m + price > ulimit + _EPS:
                    warn = f"\n⚠️ 你在 {pid} 的今日额度已用完，切换后暂时无法用它对话。"
                elif tlimit > 0 and total + price > tlimit + _EPS:
                    warn = "\n⚠️ 你的今日消费总额度已用完，切换后暂时无法用它对话。"
                elif glimit > 0 and gspent + price > glimit + _EPS:
                    warn = f"\n⚠️ {pid} 的全用户总限额已用完，切换后暂时无法用它对话。"
            scope = "本群会话" if not event.is_private_chat() else "当前私聊会话"
            yield event.plain_result(
                f"✅ 已切换到 {idx}. {pid}{suffix}，对{scope}生效。{warn}"
            )
            return

        yield event.plain_result(self._model_help())

    # ------------------------------------------------------------------
    # /quota 指令
    # ------------------------------------------------------------------

    def _quota_help(self) -> str:
        return (
            "📊 限额查询指令\n"
            "/quota —— 查看我今日各模型剩余额度（含总池剩余）\n"
            "/quota all ——（管理员）查看今日全量用量\n"
            "/quota usage [用户ID] ——（管理员）查看用量统计\n"
            "/quota reset [用户ID] ——（管理员）重置今日计数"
        )

    @filter.command("quota", alias={"额度", "限额"})
    async def quota(
        self,
        event: AstrMessageEvent,
        sub: str | None = None,
        arg: str | None = None,
    ):
        """查询当日限额 / 管理用量。"""
        s = (sub or "").strip().lower()

        if s in ("", "me", "my", "我的"):
            data = await self._load_usage()
            ukey = self._user_key(event)
            uinfo = data["users"].get(ukey, {})
            counts = uinfo.get("counts", {}) if isinstance(uinfo, dict) else {}
            spent = uinfo.get("spent", {}) if isinstance(uinfo, dict) else {}
            if not isinstance(counts, dict):
                counts = {}
            if not isinstance(spent, dict):
                spent = {}
            total = self._user_total(uinfo if isinstance(uinfo, dict) else {})
            # 图片卡（/ocgo 同款）：成功则只发图，失败回退文本
            card = await self._personal_quota_card(
                event, counts, spent, data.get("global", {}), total
            )
            if card:
                yield event.image_result(card)
                return
            lines = [f"📊 我今日剩余额度（1$≈{self.cny(1)}）："]
            rem_lines = self._remaining_lines(
                counts, spent, data.get("global", {}), data.get("global_counts", {})
            )
            lines.extend(rem_lines if rem_lines else ["当前还没有开放可自选的模型。"])
            lines.append(self._total_line(total))
            lines.append(self._reset_line())
            yield event.plain_result("\n".join(lines))
            return

        # 以下均为管理员功能
        if not event.is_admin():
            yield event.plain_result("❌ 该子命令仅限管理员使用，你可以用 /quota 查自己的额度。")
            return

        data = await self._load_usage()
        users = data["users"] if isinstance(data.get("users"), dict) else {}
        gspent_map = data["global"] if isinstance(data.get("global"), dict) else {}
        gcount_map = (
            data["global_counts"] if isinstance(data.get("global_counts"), dict) else {}
        )

        if s == "all":
            lines = [f"📊 今日全量用量（{data.get('date', '')}，共 {len(users)} 人用过）："]
            items = self._provider_list()
            known = {pid for pid, _ in items}
            for pid in list(known) + [k for k in gspent_map if k not in known]:
                model = next((m for i, m in items if i == pid), "")
                suffix = f"（{model}）" if model and model != pid else ""
                gspent = self._num(gspent_map, pid)
                gused_n = gcount_map.get(pid, 0)
                lines.append(
                    f"- {pid}{suffix}：单价 ${self.price_for(pid):.4f}/次，"
                    f"总池已花 {self.cny(gspent)}/{self._fmt_limit_usd(self.global_limit(pid), self.cny)}"
                    f"（{gused_n} 次），每人 {self._fmt_limit_usd(self.user_model_limit(pid), self.cny)}"
                )
            lines.append(
                f"每人每日总额度：{self._fmt_limit_usd(self.default_user_total_quota, self.cny)}"
                f"（1$≈{self.cny(1)}）"
            )
            if not items and not gspent_map:
                lines.append("暂无可用模型，也暂无用量。")
            yield event.plain_result("\n".join(lines))
            return

        if s == "usage":
            if arg:
                key = arg.strip()
                matched = [
                    (uk, info)
                    for uk, info in users.items()
                    if isinstance(info, dict) and (key in uk or uk.endswith(f":{key}"))
                ]
                if not matched:
                    yield event.plain_result(f"今日没有找到用户「{key}」的用量记录。")
                    return
                lines = [f"📊 用户用量（今日，共 {len(matched)} 条匹配）："]
                for uk, info in matched[:20]:
                    counts = info.get("counts", {}) if isinstance(info, dict) else {}
                    spent = info.get("spent", {}) if isinstance(info, dict) else {}
                    name = info.get("name", "") if isinstance(info, dict) else ""
                    total = info.get("total", 0.0) if isinstance(info, dict) else 0.0
                    total = total if isinstance(total, (int, float)) else 0.0
                    detail = ", ".join(
                        f"{pid}:{n}次·{self.cny(self._num(spent if isinstance(spent, dict) else {}, pid))}"
                        for pid, n in (counts.items() if isinstance(counts, dict) else [])
                    )
                    lines.append(
                        f"- {name}（{uk}）：{detail or '无'}，总花 {self.cny(total)}"
                    )
                if len(matched) > 20:
                    lines.append(f"…还有 {len(matched) - 20} 条未显示，请缩小查询范围。")
                yield event.plain_result("\n".join(lines))
                return
            lines = [f"📊 今日用量统计（{data.get('date', '')}）："]
            for pid, gspent in gspent_map.items():
                users_used = sum(
                    1
                    for info in users.values()
                    if isinstance(info, dict)
                    and isinstance(info.get("spent"), dict)
                    and self._num(info["spent"], pid) > 0
                )
                gused_n = gcount_map.get(pid, 0)
                lines.append(
                    f"- {pid}：总花 {self.cny(gspent if isinstance(gspent, (int, float)) else 0)}"
                    f"/{self._fmt_limit_usd(self.global_limit(pid), self.cny)}"
                    f"（{gused_n} 次，{users_used} 人用过）"
                )
            if not gspent_map and not gcount_map:
                lines.append("今日暂无用量。")
            else:
                lines.append(f"共 {len(users)} 人产生过记录。")
            yield event.plain_result("\n".join(lines))
            return

        if s == "reset":
            if arg:
                key = arg.strip()
                matched = [
                    uk
                    for uk, info in users.items()
                    if key in uk or uk.endswith(f":{key}")
                ]
                if not matched:
                    yield event.plain_result(f"今日没有找到用户「{key}」的用量记录，无需重置。")
                    return
                for uk in matched:
                    users.pop(uk, None)
                self._recount_global(data)
                await self._save_usage(data)
                yield event.plain_result(f"✅ 已重置 {len(matched)} 条用户今日计数并重算总池。")
                return
            await self.put_kv_data(
                _USAGE_KEY,
                {"date": self._today(), "users": {}, "global": {}, "global_counts": {}},
            )
            yield event.plain_result("✅ 已清空今日全用户计数（个人 + 总池）。")
            return

        yield event.plain_result(self._quota_help())

    async def terminate(self):
        """卸载时无需清理外部资源。"""
