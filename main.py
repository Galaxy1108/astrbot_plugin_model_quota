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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

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

_USAGE_KEY = "daily_usage_v3"
"""KV 存储中用量数据的键名（v3：按 bot 分池）。"""

_THINK_KEY = "think_efforts_v1"
"""KV 存储中「模型 -> 思考强度」的键名。"""

PLUGIN_NAME = "astrbot_plugin_model_quota"
"""插件名（Web API 路由前缀）。"""

_CONV_KEY = "conversations_v1"
"""KV 存储中「对话索引」的键名：umo -> 会话元数据。"""

_CONV_TOUCH_SECONDS = 60
"""同一对话最快多久更新一次索引，避免每条消息都写存储。"""

THINK_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "max")
"""可选思考强度；off 映射为 API 的 none。"""

THINK_OFF_VALUE = "none"
"""off 对应写入 provider 的值。"""
"""KV 存储中用量数据的键名（v2：金额制）。"""

_UNLIMITED = "不限"
"""无限制时的展示文案。"""

_EPS = 1e-9
"""浮点比较容差。"""

# ----------------------------------------------------------------------
# OpenCode Go 内置单价表（按次估算）
#
# 来源：https://opencode.ai/docs/zh-cn/go/ 的「每月限制」与「预估请求数」。
# 单次单价 = 每月限额 ÷ 每月预估请求数，即把该套餐的额度换算成一次对话的成本。
# 峰谷定价：DeepSeek 系列峰时（周一至五 01:00-04:00、06:00-10:00 UTC）价格为 2×，
# 其余时段（含周末）为低谷价；表中数值为低谷价。
# ----------------------------------------------------------------------
_OPENCODE_GO_PRICES: dict[str, float] = {
    "glm-5.3-flash": 60 / 31580,
    "glm-5.3": 15 / 1080,
    "glm-5.2": 60 / 4300,
    "glm-5.1": 60 / 4300,
    "kimi-k3": 15 / 490,
    "kimi-k2.7-code": 60 / 6750,
    "kimi-k2.6": 60 / 5750,
    "longcat-2.0": 60 / 57200,
    "mimo-v2.6-flash": 60 / 150400,
    "mimo-v2.6-pro": 15 / 16300,
    "mimo-v2.5": 60 / 150400,
    "mimo-v2.5-pro": 15 / 16300,
    "minimax-m3": 60 / 16000,
    "minimax-m2.7": 60 / 17000,
    "minimax-m2.5": 60 / 17000,
    "muse-spark-1.3-contributor": 60 / 226600,
    "muse-spark-1.2-contributor": 60 / 226600,
    "qwen3.8-max": 15 / 810,
    "qwen3.8-flash": 30 / 27000,
    "qwen3.7-max": 30 / 840,
    "qwen3.7-plus": 60 / 21600,
    "qwen3.6-plus": 60 / 16300,
    "deepseek-v4.1-flash": 60 / 130000,
    "deepseek-v4-pro": 15 / 5200,
    "deepseek-v4-flash": 30 / 65000,
    "deepseek-v4-flash-vision-exp": 15 / 32500,
    "hy4-preview": 30 / 6770,
    "hy3": 60 / 21500,
    "grok-4.7": 15 / 845,
    "grok-4.6": 15 / 845,
    "gpt-6-luna": 15 / 21130,
    "gpt-5.6-luna": 15 / 10250,
    "space-bunny-free": 0.0,
}
"""模型名（规范化后）-> 低谷期单次调用单价（美元）。"""

_OPENCODE_GO_PEAK_MODELS: frozenset[str] = frozenset(
    {
        "deepseek-v4.1-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }
)
"""内置表中参与峰谷定价的模型（DeepSeek 系列）。"""

_OPENCODE_GO_MONTHLY_LIMITS: dict[str, float] = {
    "glm-5.3-flash": 60, "glm-5.3": 15, "glm-5.2": 60, "glm-5.1": 60,
    "kimi-k3": 15, "kimi-k2.7-code": 60, "kimi-k2.6": 60, "longcat-2.0": 60,
    "mimo-v2.6-flash": 60, "mimo-v2.6-pro": 15, "mimo-v2.5": 60,
    "mimo-v2.5-pro": 15, "minimax-m3": 60, "minimax-m2.7": 60,
    "minimax-m2.5": 60, "muse-spark-1.3-contributor": 60,
    "muse-spark-1.2-contributor": 60, "qwen3.8-max": 15, "qwen3.8-flash": 30,
    "qwen3.7-max": 30, "qwen3.7-plus": 60, "qwen3.6-plus": 60,
    "deepseek-v4.1-flash": 60, "deepseek-v4-pro": 15, "deepseek-v4-flash": 30,
    "deepseek-v4-flash-vision-exp": 15, "hy4-preview": 30, "hy3": 60,
    "grok-4.7": 15, "grok-4.6": 15, "gpt-6-luna": 15, "gpt-5.6-luna": 15,
    "space-bunny-free": 0,
}
"""OpenCode Go 各模型的每月额度（美元），用于文档与月→日换算参考。"""

PRICING_PRESET_OPENCODE_GO = "opencode_go"
"""内置单价预设名。"""

QUOTA_PRESET_OPENCODE_GO = "opencode_go"
"""内置限额预设名：按官方每月额度 ÷ 60 换算成每人每日额度。"""

_QUOTA_PRESET_DEFAULT_GLOBAL = 1.0
"""限额预设下未命中月额度表时的总池默认值（美元）。"""

_GROUP_ADMIN_ROLES: frozenset[str] = frozenset({"owner", "admin"})
"""OneBot 11 群成员角色里算「群管理员」的取值（owner=群主，admin=管理员）。"""

_ALL_USERS_TOTAL_DEFAULT = 2.0
"""全用户每日最大总额（本 bot 所有用户合计，美元）。"""

_QUOTA_POOL_TIER_MONTHLY = 15.0
"""保留每模型总池的档位：官方月额度 $15。"""

_QUOTA_POOL_TIER_AMOUNT = 1.0
"""$15 档模型的每 bot 总池（美元）。"""

_QUOTA_POOL_UNLIMITED = 0.0
"""其余档位不再单设总池：由「全用户总额」统一约束。"""

_QUOTA_PRESET_DEFAULT_TOTAL = 1.5
"""限额预设下每人每天消费总额度（美元）。"""


class QuotaRow(NamedTuple):
    """额度卡片的一行。

    Attributes:
        label: 行标题（模型名或「个人总额」）。
        percent: 已用百分比（0-100，进度条长度）。
        limited: 是否已用完（决定进度条颜色与 ※ 标记）。
        sub_left: 进度条下方左侧说明。
        sub_right: 进度条下方右侧说明。
        pool_percent: 总池已用百分比；None 表示该行没有总池条。
        pool_sub: 总池说明（当前由 sub_left 承载，保留字段）。
        weight: 进度条粗细等级。0=普通模型行，1=中等（全用户总额），
            2=最粗（个人总额）。
    """

    label: str
    percent: float
    limited: bool
    sub_left: str
    sub_right: str
    pool_percent: float | None = None
    pool_sub: str = ""
    weight: int = 0

    @property
    def thick(self) -> bool:
        """兼容旧调用：是否使用加粗条。"""
        return self.weight >= 2

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
        raw_unselectable = cfg.get("unselectable_models", [])
        self.unselectable_models: set[str] = (
            {self._norm_name(str(x)) for x in raw_unselectable if str(x).strip()}
            if isinstance(raw_unselectable, list)
            else set()
        )
        self._warned_unknown_selectable = False
        self._warned_unselectable = False
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
        self.pricing_preset: str = (
            str(cfg.get("pricing_preset", PRICING_PRESET_OPENCODE_GO) or "")
            .strip()
            .lower()
        )
        self.quota_preset: str = (
            str(cfg.get("quota_preset", QUOTA_PRESET_OPENCODE_GO) or "").strip().lower()
        )
        # 峰谷定价
        self.peak_enabled: bool = bool(cfg.get("peak_pricing_enabled", True))
        self.peak_multiplier: float = self._to_float(
            cfg.get("peak_multiplier", 2.0), 2.0
        )
        if self.peak_multiplier <= 0:
            self.peak_multiplier = 2.0
        raw_windows = cfg.get("peak_windows", ["01:00-04:00", "06:00-10:00"])
        self.peak_windows: list[tuple[int, int]] = self._parse_windows(raw_windows)
        self.peak_tz_offset: float = self._to_float(
            cfg.get("peak_timezone_offset", 0.0), 0.0
        )
        self.peak_weekdays_only: bool = bool(cfg.get("peak_weekdays_only", True))
        raw_peak_models = cfg.get("peak_models", [])
        self.peak_models: set[str] = (
            {self._norm_name(str(x)) for x in raw_peak_models}
            if isinstance(raw_peak_models, list)
            else set()
        )
        self.model_peak_prices: dict[str, float] = self._to_float_map(
            cfg.get("model_peak_prices_usd", {})
        )
        self.default_user_model_quota: float = self._to_float(
            cfg.get("default_user_model_quota_usd", 1.0), 1.0
        )
        self.model_user_quotas: dict[str, float] = self._to_float_map(
            cfg.get("model_user_quotas_usd", {})
        )
        self.default_user_total_quota: float = self._to_float(
            cfg.get("default_user_total_quota_usd", _QUOTA_PRESET_DEFAULT_TOTAL),
            _QUOTA_PRESET_DEFAULT_TOTAL,
        )
        self.default_global_quota: float = self._to_float(
            cfg.get("default_global_quota_usd", _QUOTA_PRESET_DEFAULT_GLOBAL),
            _QUOTA_PRESET_DEFAULT_GLOBAL,
        )
        self.model_global_quotas: dict[str, float] = self._to_float_map(
            cfg.get("model_global_quotas_usd", {})
        )
        self.rate: float = self._to_float(cfg.get("usd_to_cny_rate", 7.2), 7.2)
        if self.rate <= 0:
            self.rate = 7.2
        self.all_users_total_quota: float = self._to_float(
            cfg.get("all_users_total_quota_usd", _ALL_USERS_TOTAL_DEFAULT),
            _ALL_USERS_TOTAL_DEFAULT,
        )
        self.admin_exempt: bool = bool(cfg.get("admin_exempt", True))
        # 思考强度
        self.think_enabled: bool = bool(cfg.get("think_enabled", True))
        self.think_admin_only: bool = bool(cfg.get("think_admin_only", True))
        # 允许群管理员（群主/群管理，非 Bot 管理员）切换模型与思考强度
        self.group_admins_can_switch: bool = bool(
            cfg.get("group_admins_can_switch", False)
        )
        raw_levels = cfg.get("think_levels", list(THINK_LEVELS))
        levels = (
            [str(x).strip().lower() for x in raw_levels if str(x).strip()]
            if isinstance(raw_levels, list)
            else list(THINK_LEVELS)
        )
        self.think_levels: list[str] = levels or list(THINK_LEVELS)
        self.default_think_effort: str = (
            str(cfg.get("default_think_effort", "") or "").strip().lower()
        )
        self._think_efforts: dict[str, str] | None = None
        self._think_original: dict[str, object] = {}
        """记录被本插件改写前的 reasoning_effort，reset 时还原。"""
        # 只在 OpenCode 预设下展示 OpenCode 提供商的模型
        self.opencode_only_models: bool = bool(cfg.get("opencode_only_models", True))
        self.opencode_api_base_match: str = (
            str(cfg.get("opencode_api_base_match", "opencode.ai") or "").strip().lower()
        )
        self._conversations: dict[str, dict] | None = None
        self._warned_no_opencode = False
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
        self.all_users_exhausted_tip: str = str(
            cfg.get(
                "all_users_exhausted_tip",
                "🈵 今天本 bot 的全用户总额度已用完（已花 {spent}/{limit}），"
                "明天再来吧～",
            )
        )
        self.global_exhausted_tip: str = str(
            cfg.get(
                "global_exhausted_tip",
                "🈵 {model} 今天的全用户总限额已用完（已花 {spent}/{limit}），"
                "明天再来吧～也可以 /model 换个模型试试。",
            )
        )

        # 插件页面（WebUI）后端接口
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/panel/overview",
                self.web_overview,
                ["GET"],
                "模型与额度总览",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/panel/reset",
                self.web_reset,
                ["POST"],
                "重置某模型的额度用量",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/panel/think",
                self.web_set_think,
                ["POST"],
                "设置某模型的思考强度",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"model_quota: 注册插件页面接口失败: {e}")

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

    def price_for(self, provider_id: str, model: str = "") -> float:
        """当前时段单次调用单价（美元），0 表示免费。

        优先级：model_call_prices_usd 显式配置 > 内置预设（按模型名匹配）>
        default_call_price_usd。峰谷定价在低谷价基础上乘 peak_multiplier。
        """
        base = self.base_price_for(provider_id, model)
        if base <= 0:
            return base
        if self.is_peak_now(provider_id, model):
            override = self._peak_price_override(provider_id, model)
            if override is not None:
                return override
            return base * self.peak_multiplier
        return base

    def base_price_for(self, provider_id: str, model: str = "") -> float:
        """低谷期（基础）单价（美元）。"""
        if provider_id in self.model_prices:
            return self.model_prices[provider_id]
        if self.pricing_preset == PRICING_PRESET_OPENCODE_GO:
            hit = self._preset_lookup(provider_id, model)
            if hit is not None:
                return hit
        return self.default_price

    def _preset_lookup(self, provider_id: str, model: str) -> float | None:
        """在内置 OpenCode Go 表中按模型名/ID 匹配。"""
        for candidate in (model, provider_id):
            key = self._norm_name(candidate)
            if not key:
                continue
            if key in _OPENCODE_GO_PRICES:
                return _OPENCODE_GO_PRICES[key]
            # 宽松匹配：去掉 - . 空格后比较（"Kimi K3" == "kimi-k3"）
            loose = key.replace("-", "").replace(".", "")
            for name, price in _OPENCODE_GO_PRICES.items():
                if name.replace("-", "").replace(".", "") == loose:
                    return price
        return None

    @staticmethod
    def _norm_name(value: str) -> str:
        """规范化模型名：小写、去 opencode-go/ 前缀、空格与下划线转连字符。"""
        text = (value or "").strip().lower()
        if "/" in text:
            text = text.rsplit("/", 1)[-1]
        return text.replace(" ", "-").replace("_", "-")

    def _preset_key(self, provider_id: str, model: str) -> str | None:
        """取得该模型在内置表中的规范键（用于峰谷判断）。"""
        for candidate in (model, provider_id):
            key = self._norm_name(candidate)
            if key in _OPENCODE_GO_PRICES:
                return key
            loose = key.replace("-", "").replace(".", "")
            for name in _OPENCODE_GO_PRICES:
                if name.replace("-", "").replace(".", "") == loose:
                    return name
        return None

    def has_peak_pricing(self, provider_id: str, model: str = "") -> bool:
        """该模型是否参与峰谷定价。"""
        if self._peak_price_override(provider_id, model) is not None:
            return True
        key = self._preset_key(provider_id, model)
        if key and key in _OPENCODE_GO_PEAK_MODELS:
            return True
        # peak_models 里写提供商 ID 或模型名都算命中
        return bool(
            {self._norm_name(model), self._norm_name(provider_id)} & self.peak_models
        )

    def _peak_price_override(self, provider_id: str, model: str) -> float | None:
        """显式配置的峰时单价（model_peak_prices_usd），没有则返回 None。"""
        if provider_id in self.model_peak_prices:
            return self.model_peak_prices[provider_id]
        for candidate in (model, provider_id):
            key = self._norm_name(candidate)
            if key and key in self.model_peak_prices:
                return self.model_peak_prices[key]
        return None

    @staticmethod
    def _parse_windows(raw: object) -> list[tuple[int, int]]:
        """把 ["01:00-04:00", ...] 解析成 [(分钟起点, 分钟终点), ...]，跨天允许。"""
        windows: list[tuple[int, int]] = []
        if not isinstance(raw, list):
            return windows
        for item in raw:
            text = str(item or "").strip()
            if "-" not in text:
                logger.warning(f"model_quota: 峰时区间 {text!r} 格式不对，应为 HH:MM-HH:MM")
                continue
            left, _, right = text.partition("-")
            try:
                sh, sm = (int(x) for x in left.strip().split(":"))
                eh, em = (int(x) for x in right.strip().split(":"))
            except (TypeError, ValueError):
                logger.warning(f"model_quota: 峰时区间 {text!r} 解析失败，已忽略")
                continue
            start = (sh % 24) * 60 + (sm % 60)
            end = (eh % 24) * 60 + (em % 60)
            windows.append((start, end))
        return windows

    def is_peak_now(self, provider_id: str, model: str = "") -> bool:
        """当前是否处于峰时（该模型参与峰谷定价时才可能为真）。"""
        if not self.peak_enabled or not self.peak_windows:
            return False
        if not self.has_peak_pricing(provider_id, model):
            return False
        if self.peak_weekdays_only and datetime.now(
            timezone(timedelta(hours=self.peak_tz_offset))
        ).weekday() >= 5:
            return False  # 周末全为低谷
        local = datetime.now(timezone(timedelta(hours=self.peak_tz_offset)))
        minutes = local.hour * 60 + local.minute
        for start, end in self.peak_windows:
            if start <= end:
                if start <= minutes < end:
                    return True
            elif minutes >= start or minutes < end:  # 跨天区间
                return True
        return False

    def peak_state_label(self, provider_id: str, model: str = "") -> str:
        """当前时段的展示文案。"""
        if not self.has_peak_pricing(provider_id, model):
            return ""
        if not self.peak_enabled or not self.peak_windows:
            return ""
        if self.is_peak_now(provider_id, model):
            return f"峰时 {self.peak_multiplier:g}×"
        return "谷时 1×"

    @staticmethod
    def _raw_sender_role(event: AstrMessageEvent) -> str:
        """从平台原始事件里取发送者的群角色。

        OneBot 11（aiocqhttp）的群消息事件里带 ``sender.role``：
        ``owner`` / ``admin`` / ``member``。读它不需要额外 API 请求。
        """
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        except Exception:  # noqa: BLE001
            return ""
        if raw is None:
            return ""
        sender = raw.get("sender") if isinstance(raw, dict) else None
        if sender is None:
            getter = getattr(raw, "get", None)
            if callable(getter):
                try:
                    sender = getter("sender")
                except Exception:  # noqa: BLE001
                    sender = None
        if not isinstance(sender, dict):
            return ""
        role = sender.get("role")
        return str(role).strip().lower() if role else ""

    async def is_group_admin(self, event: AstrMessageEvent) -> bool:
        """发送者是否为该群的群主/群管理员（与 Bot 管理员无关）。

        优先读原始事件的 ``sender.role``（零成本）；拿不到时回退到
        ``event.get_group()``（会调用平台的群成员列表接口）。
        """
        try:
            if event.is_private_chat():
                return False
        except Exception:  # noqa: BLE001
            return False

        role = self._raw_sender_role(event)
        if role:
            return role in _GROUP_ADMIN_ROLES

        # 回退：拉群信息
        try:
            group = await event.get_group()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"model_quota: 获取群信息失败，无法判定群管理员: {e}")
            return False
        if group is None:
            return False
        sender_id = str(event.get_sender_id() or "")
        if not sender_id:
            return False
        owner = str(getattr(group, "group_owner", "") or "")
        admins = getattr(group, "group_admins", None) or []
        return sender_id == owner or sender_id in {str(a) for a in admins}

    async def can_manage_models(self, event: AstrMessageEvent) -> bool:
        """是否有权切换模型 / 修改思考强度。

        Bot 管理员始终可以；开启 ``group_admins_can_switch`` 后，
        群聊里的群主/群管理员也可以（私聊不受此选项影响，本来谁都能切）。
        """
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        if not self.group_admins_can_switch:
            return False
        return await self.is_group_admin(event)

    def is_exempt(self, event: AstrMessageEvent) -> bool:
        """该用户是否免限额（管理员 + admin_exempt）。"""
        try:
            return bool(self.admin_exempt and event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def _exempt_lines(self, event: AstrMessageEvent) -> list[str]:
        """免限额用户看到的说明，避免把「0 次」误读成统计坏了。"""
        if not self.is_exempt(event):
            return []
        return [
            "👑 你是管理员：免限额、用量不计数。",
            "   下面的「已用/已花」只统计普通用户；想看到计数请用普通账号测试，",
            "   或把插件配置里的 admin_exempt 关掉（那样管理员也会被限额）。",
        ]

    def _all_users_line(self, bot_total: float) -> str:
        """文本版的「全用户总额」行（中等进度条）。"""
        alimit = self.all_users_total_quota
        if alimit > 0:
            pct = min(bot_total / alimit * 100.0, 100.0)
            line = (
                f"👥 全用户总额 {self._bar(pct)} {pct:>3.0f}% "
                f"已花 {self.cny(bot_total)}/{self.cny(alimit)}"
                f" 剩 {self.cny(max(alimit - bot_total, 0))}"
            )
            if bot_total >= alimit - _EPS:
                line += f"  {LIMITED_MARK} 已用完"
            return line
        return f"👥 全用户总额：已花 {self.cny(bot_total)}（不限）"

    def _peak_note_lines(self) -> list[str]:
        """文本输出里附一行峰谷状态（无峰谷模型时为空）。"""
        note = self._peak_summary_note()
        return [f"🕐 {note}"] if note else []

    def _peak_summary_note(self, include_models: bool = True) -> str:
        """峰谷总览文案。

        Args:
            include_models: 是否附上适用的模型名（聊天文本用 True；
                卡片里因为每个模型自带峰谷标注，用 False 更简洁）。
        """
        if not self.peak_enabled or not self.peak_windows:
            return ""
        window_text = "、".join(
            f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
            for s, e in self.peak_windows
        )
        tz_sign = "+" if self.peak_tz_offset >= 0 else "-"
        tz_text = f"UTC{tz_sign}{abs(self.peak_tz_offset):g}"
        day_text = "周一至五" if self.peak_weekdays_only else "每天"
        peak_models = [
            (pid, model)
            for pid, model in self._provider_list(selectable_only=True)
            if self.has_peak_pricing(pid, model)
        ]
        peak_now = any(self.is_peak_now(pid, model) for pid, model in peak_models)
        state = "峰时" if peak_now else "谷时"
        note = f"当前{state}；峰时 {self.peak_multiplier:g}×（{day_text} {window_text} {tz_text}）"
        if include_models and peak_models:
            names = [self._display_name(pid, model) for pid, model in peak_models]
            shown = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
            note += f"，适用：{shown}"
        return note

    def peak_row_suffix(self, provider_id: str, model: str = "") -> str:
        """卡片行尾的峰谷标注，如「 · 峰时2×（当前）」。"""
        if not self.peak_enabled or not self.peak_windows:
            return ""
        if not self.has_peak_pricing(provider_id, model):
            return ""
        if self.is_peak_now(provider_id, model):
            return f" · 峰时 {self.peak_multiplier:g}×（当前）"
        return f" · 谷时（峰时 {self.peak_multiplier:g}×）"

    def user_model_limit(self, provider_id: str, model: str = "") -> float:
        """某模型每人每日限额（美元，<=0 不限）。

        优先级：model_user_quotas_usd 显式配置 > 内置限额预设（按官方月额度 ÷ 60）
        > default_user_model_quota_usd。
        """
        if provider_id in self.model_user_quotas:
            return self.model_user_quotas[provider_id]
        if self.quota_preset == QUOTA_PRESET_OPENCODE_GO:
            monthly = self._preset_monthly_limit(provider_id, model)
            if monthly is not None:
                # 官方月额度 ÷ 60 得到每人每日额度：$60 -> $1，$30 -> $0.5，$15 -> $0.25
                return monthly / 60.0
        return self.default_user_model_quota

    def _preset_monthly_limit(self, provider_id: str, model: str) -> float | None:
        """取该模型在 OpenCode Go 的每月额度（美元），未知返回 None。"""
        key = self._preset_key(provider_id, model)
        if key is None:
            return None
        return _OPENCODE_GO_MONTHLY_LIMITS.get(key)

    def global_limit(self, provider_id: str, model: str = "") -> float:
        """某模型在单个 bot 上的每日总池（美元，<=0 不限）。

        优先级：model_global_quotas_usd 显式配置 > 内置限额预设
        （官方月额度 ÷ 15，即 $15→$1、$30→$2、$60→$4）> default_global_quota_usd。
        """
        if provider_id in self.model_global_quotas:
            return self.model_global_quotas[provider_id]
        if self.quota_preset == QUOTA_PRESET_OPENCODE_GO:
            monthly = self._preset_monthly_limit(provider_id, model)
            if monthly is not None:
                # 只有 $15 档保留每模型总池 $1；$30/$60 档的额度远大于
                # 「全用户总额」，单设总池永远不会触发，交给全用户总额统一约束。
                if monthly == _QUOTA_POOL_TIER_MONTHLY:
                    return _QUOTA_POOL_TIER_AMOUNT
                return _QUOTA_POOL_UNLIMITED
        return self.default_global_quota

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
    # 用量存储
    #
    # 记录按「用户 -> bot -> 用量」嵌套，总池按 bot 聚合：
    # {
    #   "date": "YYYY-MM-DD",
    #   "records": {
    #       "<平台:用户ID>": {
    #           "<bot 自身ID>": {
    #               "name": "昵称",
    #               "counts": {provider_id: 次数},
    #               "spent":  {provider_id: 美元},
    #           }
    #       }
    #   }
    # }
    # 个人限额跨 bot 汇总（同一人换 bot 也共用「每人每天」额度）；
    # 总池只统计当前 bot 自己的用量，各 bot 互不影响。
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().date().isoformat()

    @staticmethod
    def _user_key(event: AstrMessageEvent) -> str:
        """按人标识：平台名 + 发送者 ID（群私聊通用，不含 bot）。"""
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    @staticmethod
    def _bot_id(event: AstrMessageEvent) -> str:
        """当前 bot 自身 ID（每个 bot 一个独立总池）。"""
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        return self_id or "bot"

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
            or not isinstance(data.get("records"), dict)
        ):
            # 跨天、版本升级或数据异常：整体清零（天然按天重置，无需定时任务）
            data = {"date": today, "records": {}}
            await self.put_kv_data(_USAGE_KEY, data)
        return data

    async def _save_usage(self, data: dict) -> None:
        await self.put_kv_data(_USAGE_KEY, data)

    def _bot_bucket(
        self, data: dict, ukey: str, bot: str, event: AstrMessageEvent | None = None
    ) -> dict:
        """取出（必要时创建）某用户在某 bot 下的用量桶。"""
        records = data["records"]
        per_user = records.get(ukey)
        if not isinstance(per_user, dict):
            per_user = {}
            records[ukey] = per_user
        bucket = per_user.get(bot)
        if not isinstance(bucket, dict):
            bucket = {"name": "", "counts": {}, "spent": {}, "umo": ""}
            per_user[bot] = bucket
        if event is not None:
            try:
                umo = str(event.unified_msg_origin or "")
                if umo:
                    bucket["umo"] = umo
            except Exception:
                pass
        for k in ("counts", "spent"):
            if not isinstance(bucket.get(k), dict):
                bucket[k] = {}
        if event is not None:
            try:
                name = event.get_sender_name()
                if name:
                    bucket["name"] = name
            except Exception:
                pass
        return bucket

    def _display_name_of(self, data: dict, ukey: str) -> str:
        """从记录里取该用户的昵称（任一 bot 下有值即可）。"""
        per_user = data.get("records", {}).get(ukey, {})
        if isinstance(per_user, dict):
            for bucket in per_user.values():
                if isinstance(bucket, dict) and bucket.get("name"):
                    return str(bucket["name"])
        return ""

    def _personal_totals(self, data: dict, ukey: str) -> tuple[dict, dict, float]:
        """某人今日跨 bot 汇总：(次数表, 花费表, 总花费)。"""
        counts: dict[str, int] = {}
        spent: dict[str, float] = {}
        total = 0.0
        per_user = data.get("records", {}).get(ukey, {})
        if isinstance(per_user, dict):
            for bucket in per_user.values():
                if not isinstance(bucket, dict):
                    continue
                bcounts = bucket.get("counts", {})
                if isinstance(bcounts, dict):
                    for pid, n in bcounts.items():
                        if isinstance(n, int) and n > 0:
                            counts[pid] = counts.get(pid, 0) + n
                bspent = bucket.get("spent", {})
                if isinstance(bspent, dict):
                    for pid, v in bspent.items():
                        if isinstance(v, (int, float)) and v > 0:
                            spent[pid] = round(spent.get(pid, 0.0) + v, 10)
                            total += v
        return counts, spent, round(total, 10)

    def _pool_totals(self, data: dict, bot: str) -> tuple[dict, dict]:
        """某个 bot 自己的总池：(该 bot 各模型花费表, 各模型次数表)。"""
        spent: dict[str, float] = {}
        counts: dict[str, int] = {}
        records = data.get("records", {})
        if isinstance(records, dict):
            for per_user in records.values():
                if not isinstance(per_user, dict):
                    continue
                bucket = per_user.get(bot)
                if not isinstance(bucket, dict):
                    continue
                bspent = bucket.get("spent", {})
                if isinstance(bspent, dict):
                    for pid, v in bspent.items():
                        if isinstance(v, (int, float)) and v > 0:
                            spent[pid] = round(spent.get(pid, 0.0) + v, 10)
                bcounts = bucket.get("counts", {})
                if isinstance(bcounts, dict):
                    for pid, n in bcounts.items():
                        if isinstance(n, int) and n > 0:
                            counts[pid] = counts.get(pid, 0) + n
        return spent, counts

    def _pool_of(self, data: dict, bot: str, pid: str) -> tuple[float, int]:
        """某个 bot 在某模型上的 (花费, 次数)。"""
        spent_map, count_map = self._pool_totals(data, bot)
        return self._num(spent_map, pid), int(count_map.get(pid, 0) or 0)

    # ------------------------------------------------------------------
    # 对话索引（供插件页面展示「每个对话的模型/思考强度/额度」）
    # ------------------------------------------------------------------

    async def _load_conversations(self) -> dict:
        data = await self.get_kv_data(_CONV_KEY, None)
        if not isinstance(data, dict):
            data = {}
        return data

    async def _touch_conversation(self, event: AstrMessageEvent) -> None:
        """记录/刷新一个对话（限频写入，避免每条消息都写存储）。"""
        try:
            umo = str(event.unified_msg_origin or "")
            if not umo:
                return
            convs = await self._load_conversations()
            entry = convs.get(umo)
            now = int(time.time())
            if not isinstance(entry, dict):
                entry = {}
                convs[umo] = entry
            elif now - int(entry.get("ts") or 0) < _CONV_TOUCH_SECONDS:
                return
            def _safe(fn, default=""):
                try:
                    value = fn()
                except Exception:  # noqa: BLE001
                    return default
                return default if value is None else value

            entry.update(
                {
                    "ts": now,
                    "bot": self._bot_id(event),
                    "platform": _safe(event.get_platform_name),
                    "user_key": self._user_key(event),
                    "sender": _safe(event.get_sender_name),
                    "group_id": str(_safe(event.get_group_id)),
                    "private": bool(_safe(event.is_private_chat, False)),
                }
            )
            await self.put_kv_data(_CONV_KEY, convs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"model_quota: 更新对话索引失败: {e}")

    def conversation_usage(
        self, data: dict, umo: str, bot: str | None = None
    ) -> tuple[dict, dict]:
        """某个对话当日的 (各模型花费表, 各模型次数表)，跨该对话内所有用户汇总。"""
        spent: dict[str, float] = {}
        counts: dict[str, int] = {}
        records = data.get("records", {})
        if not isinstance(records, dict):
            return spent, counts
        for per_user in records.values():
            if not isinstance(per_user, dict):
                continue
            for b, bucket in per_user.items():
                if not isinstance(bucket, dict):
                    continue
                if bot is not None and str(b) != bot:
                    continue
                if str(bucket.get("umo") or "") != umo:
                    continue
                bspent = bucket.get("spent", {})
                if isinstance(bspent, dict):
                    for pid, v in bspent.items():
                        if isinstance(v, (int, float)) and v > 0:
                            spent[pid] = round(spent.get(pid, 0.0) + v, 10)
                bcounts = bucket.get("counts", {})
                if isinstance(bcounts, dict):
                    for pid, n in bcounts.items():
                        if isinstance(n, int) and n > 0:
                            counts[pid] = counts.get(pid, 0) + n
        return spent, counts

    def conversation_users(self, data: dict, umo: str) -> list[str]:
        """用过这个对话的用户 key 列表。"""
        users: list[str] = []
        records = data.get("records", {})
        if not isinstance(records, dict):
            return users
        for ukey, per_user in records.items():
            if not isinstance(per_user, dict):
                continue
            for bucket in per_user.values():
                if isinstance(bucket, dict) and str(bucket.get("umo") or "") == umo:
                    users.append(str(ukey))
                    break
        return users

    def reset_model_usage(
        self,
        data: dict,
        provider_id: str,
        *,
        bot: str | None = None,
        umo: str | None = None,
    ) -> tuple[int, int]:
        """把某模型在指定范围内的用量清零（个人桶 + 次数）。

        总池是从桶里推导出来的，所以清零后总池自动回落。
        返回 (受影响的用户数, 被清零的桶数)。
        """
        users_hit = 0
        buckets_hit = 0
        records = data.get("records", {})
        if not isinstance(records, dict):
            return 0, 0
        for per_user in records.values():
            if not isinstance(per_user, dict):
                continue
            touched_user = False
            for b, bucket in per_user.items():
                if not isinstance(bucket, dict):
                    continue
                if bot is not None and str(b) != bot:
                    continue
                if umo is not None and str(bucket.get("umo") or "") != umo:
                    continue
                spent = bucket.get("spent")
                counts = bucket.get("counts")
                changed = False
                if isinstance(spent, dict) and provider_id in spent:
                    spent.pop(provider_id, None)
                    changed = True
                if isinstance(counts, dict) and provider_id in counts:
                    counts.pop(provider_id, None)
                    changed = True
                if changed:
                    buckets_hit += 1
                    touched_user = True
            if touched_user:
                users_hit += 1
        return users_hit, buckets_hit

    def bot_total_spent(self, data: dict, bot: str) -> float:
        """某个 bot 上所有用户、所有模型的合计花费（美元）。"""
        total = 0.0
        spent_map, _ = self._pool_totals(data, bot)
        for v in spent_map.values():
            if isinstance(v, (int, float)) and v > 0:
                total += v
        return round(total, 10)

    def _active_bots(self, data: dict) -> list[str]:
        """今日有记录的 bot 列表。"""
        bots: set[str] = set()
        records = data.get("records", {})
        if isinstance(records, dict):
            for per_user in records.values():
                if isinstance(per_user, dict):
                    bots.update(str(b) for b in per_user)
        return sorted(bots)

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
        model_name = provider.meta().model or ""
        price = self.price_for(pid, model_name)
        disp = self._display_name(pid, model_name)

        # 思考强度按模型生效，管理员也照常应用（放在豁免判断之前）
        await self._apply_think_effort(provider, pid)
        # 记录对话，供插件页面展示
        await self._touch_conversation(event)

        if self.admin_exempt and event.is_admin():
            return  # 管理员免限额且不计数

        data = await self._load_usage()
        ukey = self._user_key(event)
        bot = self._bot_id(event)
        counts, spent, total = self._personal_totals(data, ukey)
        gspent, gcount = self._pool_of(data, bot, pid)

        used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
        spent_m = self._num(spent, pid)

        if price > 0:
            ulimit = self.user_model_limit(pid, model_name)
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

            alimit = self.all_users_total_quota
            if alimit > 0:
                btotal = self.bot_total_spent(data, bot)
                if btotal + price > alimit + _EPS:
                    tip = self.all_users_exhausted_tip.format(
                        spent=self.cny(btotal), limit=self.cny(alimit)
                    )
                    await event.send(event.plain_result(tip))
                    event.stop_event()
                    return

            glimit = self.global_limit(pid, model_name)
            if glimit > 0 and gspent + price > glimit + _EPS:
                tip = self.global_exhausted_tip.format(
                    model=disp,
                    used=gcount,
                    spent=self.cny(gspent),
                    limit=self.cny(glimit),
                )
                await event.send(event.plain_result(tip))
                event.stop_event()
                return

        # 扣费放行（免费模型只计次数）；写入该用户在该 bot 下的桶
        bucket = self._bot_bucket(data, ukey, bot, event)
        bcounts: dict = bucket["counts"]
        bspent: dict = bucket["spent"]
        bcounts[pid] = (bcounts.get(pid, 0) if isinstance(bcounts.get(pid), int) else 0) + 1
        if price > 0:
            bspent[pid] = round(self._num(bspent, pid) + price, 10)
        await self._save_usage(data)

    # ------------------------------------------------------------------
    # 思考强度（reasoning_effort）
    #
    # AstrBot 的 reasoning_effort 只能通过 provider 的 custom_extra_body 生效
    # （text_chat 的 **kwargs 不会并入 payload），因此这里是「按模型」设置：
    # 改的是该模型在当前 bot 上的推理强度，所有用户共用，不是按人隔离。
    # ------------------------------------------------------------------

    async def _load_think_efforts(self) -> dict[str, str]:
        if self._think_efforts is None:
            data = await self.get_kv_data(_THINK_KEY, None)
            if not isinstance(data, dict):
                data = {}
            self._think_efforts = {
                str(k): str(v).strip().lower()
                for k, v in data.items()
                if str(v).strip()
            }
        return self._think_efforts

    async def _save_think_efforts(self) -> None:
        await self.put_kv_data(_THINK_KEY, dict(self._think_efforts or {}))

    @staticmethod
    def _think_api_value(level: str) -> str:
        """插件内的层级名 -> 写入 provider 的值。"""
        return THINK_OFF_VALUE if level == "off" else level

    async def think_effort_of(self, provider_id: str) -> str:
        """该模型当前的思考强度；空串表示未设置（跟随 provider 自身配置）。"""
        efforts = await self._load_think_efforts()
        return efforts.get(provider_id, self.default_think_effort)

    def _think_body(self, provider: object) -> dict | None:
        """取得（必要时创建）provider 的 custom_extra_body。"""
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return None
        body = config.get("custom_extra_body")
        if not isinstance(body, dict):
            body = {}
            config["custom_extra_body"] = body
        return body

    async def _apply_think_effort(self, provider: object, provider_id: str) -> None:
        """把该模型的思考强度写进 provider 的 custom_extra_body。

        未设置（或已 reset）时，若之前被本插件改写过，则还原成原值。
        """
        if not self.think_enabled:
            return
        body = self._think_body(provider)
        if body is None:
            return
        level = await self.think_effort_of(provider_id)
        if level:
            # 首次改写前记下原值，供 reset 还原
            self._think_original.setdefault(provider_id, body.get("reasoning_effort"))
            value = self._think_api_value(level)
            if body.get("reasoning_effort") != value:
                body["reasoning_effort"] = value
                logger.info(
                    f"model_quota: 已将模型 {provider_id} 的思考强度设为 {level}"
                    f"（reasoning_effort={value}）"
                )
            return
        # 未设置：还原原值
        if provider_id in self._think_original:
            original = self._think_original.pop(provider_id)
            if original is None:
                body.pop("reasoning_effort", None)
            else:
                body["reasoning_effort"] = original
            logger.info(f"model_quota: 已还原模型 {provider_id} 的思考强度设置")

    def _think_help(self, provider_id: str, model: str, current: str) -> str:
        lines = [
            f"🧠 思考强度：{self._display_name(provider_id, model)}",
            f"当前：{current or '未设置（跟随模型服务自身配置）'}",
            f"可选：{' / '.join(self.think_levels)} / reset",
            f"用法：/think <级别>（例：/think high），/think reset 清除",
        ]
        if not self.think_admin_only:
            lines.append("该设置按模型生效，同模型的所有用户共用。")
        elif self.group_admins_can_switch:
            lines.append(
                "该设置按模型生效（同模型的所有用户共用），"
                "仅 Bot 管理员或本群群主/群管理员可改。"
            )
        else:
            lines.append("该设置按模型生效（同模型的所有用户共用），仅管理员可改。")
        return "\n".join(lines)

    @filter.command("think", alias={"思考", "思考强度"})
    async def think(self, event: AstrMessageEvent, action: str | None = None):
        """查看或设置当前模型的思考强度。"""
        if not self.think_enabled:
            yield event.plain_result("思考强度功能已在插件配置中关闭。")
            return
        prov = await self._provider_of(event)
        if prov is None:
            yield event.plain_result("暂时拿不到当前模型，稍后再试。")
            return
        pid = prov.meta().id
        model = prov.meta().model or ""
        act = (action or "").strip().lower()
        if not act:
            current = await self.think_effort_of(pid)
            yield event.plain_result(self._think_help(pid, model, current))
            return
        if self.think_admin_only and not await self.can_manage_models(event):
            if self.group_admins_can_switch and not event.is_private_chat():
                hint = "❌ 思考强度仅 Bot 管理员或本群群主/群管理员可修改，你可以用 /think 查看当前值。"
            else:
                hint = "❌ 思考强度仅管理员可修改，你可以用 /think 查看当前值。"
            yield event.plain_result(hint)
            return
        efforts = await self._load_think_efforts()
        disp = self._display_name(pid, model)
        if act in ("reset", "clear", "默认", "重置"):
            efforts.pop(pid, None)
            await self._save_think_efforts()
            await self._apply_think_effort(prov, pid)
            yield event.plain_result(f"✅ 已清除 {disp} 的思考强度设置。")
            return
        if act not in self.think_levels:
            yield event.plain_result(
                f"❌ 不支持的级别「{act}」，可选：{' / '.join(self.think_levels)} / reset"
            )
            return
        efforts[pid] = act
        await self._save_think_efforts()
        await self._apply_think_effort(prov, pid)
        yield event.plain_result(f"✅ 已将 {disp} 的思考强度设为 {act}。")

    # ------------------------------------------------------------------
    # /model 指令（群聊切换仅管理员）
    # ------------------------------------------------------------------

    def _all_providers(self) -> list:
        try:
            return list(self.context.get_all_providers())
        except Exception as e:
            logger.warning(f"model_quota: 获取模型列表失败: {e}")
            return []

    def _preset_active(self) -> bool:
        """是否启用了任一 OpenCode Go 内置预设。"""
        return (
            self.pricing_preset == PRICING_PRESET_OPENCODE_GO
            or self.quota_preset == QUOTA_PRESET_OPENCODE_GO
        )

    def is_opencode_provider(self, provider: object) -> bool:
        """该 provider 是否来自 OpenCode（按 api_base / 名称判断）。"""
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return False
        match = self.opencode_api_base_match
        api_base = str(config.get("api_base") or "").lower()
        if match and match in api_base:
            return True
        for key in ("provider", "id", "name", "type"):
            if "opencode" in str(config.get(key) or "").lower():
                return True
        return False

    def _selectable_providers(self) -> list:
        """开放自选的 provider 列表（应用白名单 + OpenCode 限定）。"""
        provs = self._all_providers()
        if self.selectable_models:
            if not self._warned_unknown_selectable:
                self._warned_unknown_selectable = True
                known = {p.meta().id for p in provs}
                for pid in self.selectable_models:
                    if pid not in known:
                        logger.warning(
                            f"model_quota: selectable_models 中的 {pid!r} "
                            "在当前提供商中不存在，已忽略"
                        )
            provs = [p for p in provs if p.meta().id in self.selectable_models]
        if self.unselectable_models:
            def _excluded(p) -> bool:
                try:
                    meta = p.meta()
                except Exception:  # noqa: BLE001
                    return False
                return bool(
                    {
                        self._norm_name(meta.id),
                        self._norm_name(meta.model or ""),
                    }
                    & self.unselectable_models
                )

            kept = [p for p in provs if not _excluded(p)]
            if not self._warned_unselectable:
                self._warned_unselectable = True
                if len(kept) != len(provs):
                    logger.info(
                        f"model_quota: unselectable_models 已排除 "
                        f"{len(provs) - len(kept)} 个模型"
                    )
                known = set()
                for p in provs:
                    try:
                        meta = p.meta()
                    except Exception:  # noqa: BLE001
                        continue
                    known.add(self._norm_name(meta.id))
                    known.add(self._norm_name(meta.model or ""))
                for name in self.unselectable_models - known:
                    logger.warning(
                        f"model_quota: unselectable_models 中的 {name!r} "
                        "没有匹配到任何模型，已忽略"
                    )
            provs = kept

        if self.opencode_only_models and self._preset_active():
            oc = [p for p in provs if self.is_opencode_provider(p)]
            if oc:
                provs = oc
            elif not self._warned_no_opencode and provs:
                self._warned_no_opencode = True
                logger.warning(
                    "model_quota: 开启了 opencode_only_models，但没有找到 OpenCode "
                    f"提供商（api_base 含 {self.opencode_api_base_match!r}），"
                    "本次仍展示全部模型；如不需要可关闭该选项"
                )
        return provs

    def _provider_list(self, selectable_only: bool = False) -> list[tuple[str, str]]:
        """返回 [(provider_id, model名)]。

        Args:
            selectable_only: 为 True 时只返回开放自选的模型
                （白名单 + 预设下的 OpenCode 限定）。
        """
        provs = self._selectable_providers() if selectable_only else self._all_providers()
        items: list[tuple[str, str]] = []
        for p in provs:
            try:
                meta = p.meta()
            except Exception:  # noqa: BLE001
                continue
            items.append((meta.id, meta.model or ""))
        return items

    async def _provider_of(self, event: AstrMessageEvent):
        """当前会话正在使用的模型实例；取不到返回 None。"""
        try:
            return await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
        except Exception as e:
            logger.warning(f"model_quota: 获取当前模型失败: {e}")
            return None

    async def _current_provider_id(self, event: AstrMessageEvent) -> str | None:
        p = await self._provider_of(event)
        return p.meta().id if p else None

    def _find_provider(self, token: str) -> tuple[int, str, str] | None:
        """在开放自选的模型中查找，返回 (序号, id, model)。

        匹配顺序：序号（1 起）> 提供商 ID > 显示名 > 底层模型名，
        后三者都忽略大小写、也忽略空格与下划线/短横线的差异，
        所以 ``/model 2``、``/model Kimi K3``、``/model gpt-6-luna`` 都能用
        （调用方需用 GreedyStr 接参，才能拿到带空格的完整名称）。
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
        loose = self._norm_name(token)

        def match(candidate: str) -> bool:
            if not candidate:
                return False
            text = str(candidate)
            return text.lower() == lowered or self._norm_name(text) == loose

        for i, (pid, model) in enumerate(items, start=1):
            if match(pid):
                return i, pid, model
        for i, (pid, model) in enumerate(items, start=1):
            if match(self._display_name(pid, model)):
                return i, pid, model
        for i, (pid, model) in enumerate(items, start=1):
            if match(model):
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
            price = self.price_for(pid, model)
            title = self._display_name(pid, model)
            used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
            spent_m = self._num(spent, pid)
            if price <= 0:
                lines.append(f"- {title}：免费·{_UNLIMITED}（已用 {used_n} 次）")
                continue
            ulimit = self.user_model_limit(pid, model)
            pool_suffix = ""
            glimit = self.global_limit(pid, model)
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
                    f"{pool_suffix}{self.peak_row_suffix(pid, model)}"
                )
                if exhausted:
                    line += f"  {LIMITED_MARK} 已用完"
                lines.append(line)
            else:
                lines.append(
                    f"- {title}：已用 {used_n} 次·{self.cny(spent_m)}（个人不限）"
                    f"{pool_suffix}{self.peak_row_suffix(pid, model)}"
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
    ) -> list[QuotaRow]:
        """组装个人额度卡片行，只含开放自选的模型（个人总额行由调用方置顶）。"""
        rows: list[QuotaRow] = []
        for pid, model in self._provider_list(selectable_only=True):
            price = self.price_for(pid, model)
            label = self._display_name(pid, model)
            used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
            spent_m = self._num(spent, pid)
            peak_note = self.peak_row_suffix(pid, model)
            if price <= 0:
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=0.0,
                        limited=False,
                        sub_left=f"免费·{_UNLIMITED}（已用 {used_n} 次）{peak_note}",
                        sub_right="",
                    )
                )
                continue
            ulimit = self.user_model_limit(pid, model)
            glimit = self.global_limit(pid, model)
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
                if peak_note:
                    sub_left += peak_note
                if pool_sub:
                    sub_left += f" · {pool_sub}"
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=pct,
                        limited=exhausted,
                        sub_left=sub_left,
                        sub_right=f"剩 {self.cny(max(ulimit - spent_m, 0))}",
                        pool_percent=pool_pct,
                    )
                )
            else:
                sub_left = f"已用 {used_n} 次·{self.cny(spent_m)}（个人不限）"
                if peak_note:
                    sub_left += peak_note
                if pool_sub:
                    sub_left += f" · {pool_sub}"
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=0.0,
                        limited=False,
                        sub_left=sub_left,
                        sub_right="",
                        pool_percent=pool_pct,
                    )
                )
        return rows

    def _total_row(self, total: float) -> QuotaRow:
        """个人消费总额行（加粗进度条，固定置顶）。"""
        tlimit = self.default_user_total_quota
        if tlimit > 0:
            pct = min(total / tlimit * 100.0, 100.0)
            return QuotaRow(
                label="个人总额",
                percent=pct,
                limited=total >= tlimit - _EPS,
                sub_left=(
                    f"已花 {self.cny(total)} / 上限 {self.cny(tlimit)}"
                    f"（所有模型合计）"
                ),
                sub_right=f"剩 {self.cny(max(tlimit - total, 0))}",
                weight=2,
            )
        return QuotaRow(
            label="个人总额",
            percent=0.0,
            limited=False,
            sub_left=f"已花 {self.cny(total)}（总额不限）",
            sub_right="",
            weight=2,
        )

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
        """卡片输出路径：优先 AstrBot 数据目录，取不到时退回系统临时目录。"""
        import hashlib

        digest = hashlib.sha256(name.encode("utf-8", "ignore")).hexdigest()[:8]
        filename = f"quota_{digest}.png"
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            out_dir = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "astrbot_plugin_model_quota"
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            return out_dir / filename
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"model_quota: 数据目录不可用，改用临时目录: {exc}")
        try:
            import tempfile

            out_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_model_quota"
            out_dir.mkdir(parents=True, exist_ok=True)
            return out_dir / filename
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"model_quota: 找不到可写目录，无法渲染图片卡: {exc}")
            return None

    def _render_quota_card(
        self,
        title: str,
        name: str,
        subtitle: str,
        rows: list[QuotaRow],
        note: str = "",
    ) -> str | None:
        """用 Pillow 绘制额度卡片。返回图片路径，失败返回 None。

        Args:
            note: 右上区域的补充说明（如峰谷状态），单独一行展示。

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
        mid_h = 11 * scale
        """全用户总额行的中等进度条高度。"""
        thick_h = 15 * scale
        """个人总额行的最粗进度条高度。"""

        def row_bar_h(weight: int) -> int:
            return thick_h if weight >= 2 else (mid_h if weight == 1 else bar_h)
        pool_h = 5 * scale
        gap_label_bar = 9 * scale
        gap_bar_sub = 8 * scale
        gap_bar_pool = 6 * scale
        gap_section = 20 * scale

        probe = ImageDraw.Draw(Image.new("RGB", (width, 8)))

        def line_h(text: str, font) -> int:
            box = probe.textbbox((0, 0), text, font=font)
            return box[3] - box[1]

        def fit(text: str, font, max_width: int) -> str:
            """超宽则截断加省略号，避免文字被卡片右边缘裁掉。"""
            if not text or probe.textlength(text, font=font) <= max_width:
                return text
            trimmed = text
            while len(trimmed) > 1 and probe.textlength(
                trimmed + "…", font=font
            ) > max_width:
                trimmed = trimmed[:-1]
            return trimmed + "…"

        title_h = line_h("剩余额度", f_title)
        sub_h = line_h("个人额度 · 已用百分比", f_sub)
        meta_h = line_h("更新于 2000/00/00 00:00:00", f_meta)
        label_h = line_h("总池·provider", f_label)
        subline_h = line_h("已花 ¥000.00 / 上限 ¥000.00", f_small)

        height = pad + title_h + 10 * scale + sub_h + 6 * scale + meta_h
        if note:
            height += 5 * scale + meta_h
        height += 20 * scale
        for _row in rows:
            row_bar = row_bar_h(_row.weight)
            height += label_h + gap_label_bar + row_bar
            if _row.pool_percent is not None:
                height += gap_bar_pool + pool_h
            height += gap_bar_sub + subline_h
            height += gap_section
        if rows:
            height -= gap_section
        if any(row.limited for row in rows):
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
        title_w = probe.textlength(title, font=f_title)
        name = fit(
            name,
            f_sub,
            int(width - pad * 2 - title_w - 16 * scale),
        )
        name_w = probe.textlength(name, font=f_sub)
        draw.text(
            (width - pad - name_w, cursor + (title_h - sub_h)),
            name,
            font=f_sub,
            fill=COLOR_MUTED,
        )
        cursor += title_h + 10 * scale

        draw.text(
            (pad, cursor),
            fit(subtitle, f_sub, width - pad * 2),
            font=f_sub,
            fill=COLOR_MUTED,
        )
        cursor += sub_h + 6 * scale

        draw.text(
            (pad, cursor),
            f"更新于 {now.year}/{now.month}/{now.day} {now:%H:%M:%S} · {reset_text}",
            font=f_meta,
            fill=COLOR_MUTED,
        )
        cursor += meta_h
        if note:
            cursor += 5 * scale
            draw.text(
                (pad, cursor),
                fit(f"峰谷：{note}", f_meta, width - pad * 2),
                font=f_meta,
                fill=COLOR_MUTED,
            )
            cursor += meta_h
        cursor += 20 * scale

        track_w = width - pad * 2
        for row in rows:
            label, percent, limited = row.label, row.percent, row.limited
            sub_left, sub_right, pool_pct = row.sub_left, row.sub_right, row.pool_percent
            cur_bar_h = row_bar_h(row.weight)
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
                (pad, cursor, pad + track_w, cursor + cur_bar_h),
                radius=cur_bar_h // 2,
                fill=COLOR_TRACK,
            )
            if percent > 0:
                fill_w = max(int(track_w * percent / 100.0), cur_bar_h)
                draw.rounded_rectangle(
                    (pad, cursor, pad + fill_w, cursor + cur_bar_h),
                    radius=cur_bar_h // 2,
                    fill=color,
                )
            cursor += cur_bar_h
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

        if any(row.limited for row in rows):
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
        bot_total: float | None = None,
    ) -> str | None:
        """尝试渲染个人额度图片卡；配置为 text 或渲染失败时返回 None（调用方回退文本）。"""
        if self.quota_render == "text":
            return None
        model_rows = self._quota_card_rows_personal(counts, spent, gspent_map)
        # 置顶：个人总额（最粗）-> 全用户总额（中等）-> 各模型
        rows: list[QuotaRow] = [self._total_row(total)]
        if bot_total is not None:
            rows.append(self._all_users_row(bot_total))
        rows.extend(model_rows)
        try:
            name = event.get_sender_name() or ""
        except Exception:
            name = ""
        subtitle = f"个人额度 · 已用百分比（1$≈{self.cny(1)}）"
        if self.is_exempt(event):
            subtitle += " · 管理员免限额，用量不计数"
        card = await asyncio.to_thread(
            self._render_quota_card,
            "剩余额度",
            name,
            subtitle,
            rows,
            self._peak_summary_note(include_models=False),
        )
        if card is None and self.quota_render == "image":
            logger.warning("model_quota: 图片渲染失败（缺 Pillow 或中文字体），已回退文本")
        return card

    def _all_users_row(self, bot_total: float) -> QuotaRow:
        """全用户总额行（本 bot 所有用户合计，中等粗细）。"""
        alimit = self.all_users_total_quota
        if alimit > 0:
            pct = min(bot_total / alimit * 100.0, 100.0)
            return QuotaRow(
                label="全用户总额",
                percent=pct,
                limited=bot_total >= alimit - _EPS,
                sub_left=(
                    f"已花 {self.cny(bot_total)} / 上限 {self.cny(alimit)}"
                    f"（本 bot 所有用户合计）"
                ),
                sub_right=f"剩 {self.cny(max(alimit - bot_total, 0))}",
                weight=1,
            )
        return QuotaRow(
            label="全用户总额",
            percent=0.0,
            limited=False,
            sub_left=f"已花 {self.cny(bot_total)}（全用户总额不限）",
            sub_right="",
            weight=1,
        )

    def _model_list_rows(
        self,
        items: list[tuple[str, str]],
        current: str | None,
        counts: dict,
        spent: dict,
        gspent_map: dict,
    ) -> list[QuotaRow]:
        """可选模型列表的卡片行：每行一个模型（进度=个人已用比例）。"""
        rows: list[QuotaRow] = []
        for i, (pid, model) in enumerate(items, start=1):
            price = self.price_for(pid, model)
            name = self._display_name(pid, model)
            label = f"{i}. {name}" + ("  *" if pid == current else "")
            used_n = counts.get(pid, 0) if isinstance(counts.get(pid), int) else 0
            spent_m = self._num(spent, pid)
            if price <= 0:
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=0.0,
                        limited=False,
                        sub_left=f"免费·{_UNLIMITED}（已用 {used_n} 次）",
                        sub_right="",
                    )
                )
                continue
            ulimit = self.user_model_limit(pid, model)
            sub_left = f"${price:.4f}/次" + self.peak_row_suffix(pid, model)
            sub_left += f" · 已用 {used_n} 次"
            # 与 /quota 卡一致：有总池的模型带上池条与池用量
            glimit = self.global_limit(pid, model)
            pool_pct: float | None = None
            if glimit > 0:
                gspent = self._num(gspent_map, pid)
                pool_pct = min(gspent / glimit * 100.0, 100.0)
                sub_left += (
                    f" · 总池已花 {self.cny(gspent)}/{self.cny(glimit)}"
                    f"（剩 {self.cny(max(glimit - gspent, 0))}）"
                )
            if ulimit > 0:
                pct = min(spent_m / ulimit * 100.0, 100.0)
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=pct,
                        limited=spent_m + price > ulimit + _EPS,
                        sub_left=sub_left,
                        sub_right=f"剩 {self.cny(max(ulimit - spent_m, 0))}",
                        pool_percent=pool_pct,
                    )
                )
            else:
                rows.append(
                    QuotaRow(
                        label=label,
                        percent=0.0,
                        limited=False,
                        sub_left=sub_left,
                        sub_right="",
                        pool_percent=pool_pct,
                    )
                )
        return rows

    async def _model_list_card(
        self,
        event: AstrMessageEvent,
        items: list[tuple[str, str]],
        current: str | None,
        counts: dict,
        spent: dict,
        gspent_map: dict,
        total: float,
        bot_total: float | None = None,
    ) -> str | None:
        """渲染可选模型图片卡；配置为 text 或缺 Pillow 时返回 None。"""
        if self.quota_render == "text":
            return None
        rows: list[QuotaRow] = [self._total_row(total)]
        if bot_total is not None:
            rows.append(self._all_users_row(bot_total))
        rows.extend(
            self._model_list_rows(items, current, counts, spent, gspent_map)
        )
        try:
            name = event.get_sender_name() or ""
        except Exception:
            name = ""
        subtitle = (
            f"共 {len(items)} 个可选模型 · 用 /model use <序号> 切换"
            f"（1$≈{self.cny(1)}）"
        )
        card = await asyncio.to_thread(
            self._render_quota_card,
            "可选模型",
            name,
            subtitle,
            rows,
            self._peak_summary_note(include_models=False),
        )
        if card is None and self.quota_render == "image":
            logger.warning("model_quota: 模型列表图片渲染失败，已回退文本")
        return card

    def _model_help(self) -> str:
        return (
            "🤖 模型自选指令\n"
            "/model —— 查看可选模型、当前模型与剩余额度\n"
            "/model list —— 同上\n"
            "/model use <序号|名称> —— 切换当前对话的模型（例：/model use 2）\n"
            "  快捷写法：/model 2 或 /model Kimi K3（名称带空格也能直接写）\n"
            "/quota —— 查看我今日剩余额度（等同原来的 /model me）\n"
            "/think —— 查看或修改当前模型的思考强度\n"
            "说明：私聊谁都可以切换；"
            + (
                "群聊里 Bot 管理员或本群群主/群管理员可以切换。\n"
                if self.group_admins_can_switch
                else "群聊里只有管理员能切换。\n"
            )
            +
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
            bot = self._bot_id(event)
            counts, spent, total = self._personal_totals(data, ukey)
            gspent_map, gcount_map = self._pool_totals(data, bot)
            # 先尝试图片卡（与 /quota 同款渲染），失败回退文本
            card = await self._model_list_card(
                event,
                items,
                current,
                counts,
                spent,
                gspent_map,
                total,
                self.bot_total_spent(data, bot),
            )
            if card:
                yield event.image_result(card)
                return
            lines = ["🤖 可选 AI 模型（* 为当前对话正在用）："]
            for i, (pid, model) in enumerate(items, start=1):
                mark = " *" if pid == current else ""
                base = self.base_price_for(pid, model)
                if base <= 0:
                    fee = "免费"
                else:
                    fee = f"${base:.4f}/次"
                    if self.has_peak_pricing(pid, model) and self.peak_enabled:
                        if self.is_peak_now(pid, model):
                            fee += f"，当前峰时 {self.peak_multiplier:g}×"
                            fee += f"（${base * self.peak_multiplier:.4f}）"
                        else:
                            fee += f"，谷时；峰时 ${base * self.peak_multiplier:.4f}"
                lines.append(f"{i}. {self._display_name(pid, model)} [{fee}]{mark}")
            lines.append("")
            rem_lines = self._remaining_lines(counts, spent, gspent_map, gcount_map)
            if rem_lines:
                lines.append(f"📊 我今日剩余额度（1$≈{self.cny(1)}）：")
                lines.extend(rem_lines)
                lines.extend(self._exempt_lines(event))
                lines.append(self._total_line(total))
                lines.append(
                    self._all_users_line(self.bot_total_spent(data, bot))
                )
                lines.append(self._reset_line())
                lines.extend(self._peak_note_lines())
                lines.append("")
            lines.append("切换：/model use <序号|名称>（例：/model use 2）")
            if not event.is_private_chat():
                if self.group_admins_can_switch:
                    lines.append("群聊中切换模型：Bot 管理员或本群群主/群管理员。")
                else:
                    lines.append("群聊中切换模型仅限管理员。")
            yield event.plain_result("\n".join(lines))
            return

        if act in ("me", "my", "mine", "我的", "额度"):
            yield event.plain_result(
                "ℹ️ /model me 已移除，请改用 /quota（别名 /额度、/限额）查看今日剩余额度。"
            )
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
            # 群聊：Bot 管理员，或（开启选项后）本群群主/群管理员
            if not event.is_private_chat() and not await self.can_manage_models(event):
                if self.group_admins_can_switch:
                    tip = (
                        "❌ 群聊中切换模型仅限 Bot 管理员或本群群主/群管理员，"
                        "你可以用 /model 查看模型和剩余额度。"
                    )
                else:
                    tip = "❌ 群聊中切换模型仅限管理员，你可以用 /model 查看模型和剩余额度。"
                yield event.plain_result(tip)
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
            price = self.price_for(pid, model)
            if price > 0 and not (self.admin_exempt and event.is_admin()):
                data = await self._load_usage()
                ukey = self._user_key(event)
                bot = self._bot_id(event)
                _, spent, total = self._personal_totals(data, ukey)
                spent_m = self._num(spent, pid)
                gspent, _ = self._pool_of(data, bot, pid)
                ulimit = self.user_model_limit(pid, model)
                tlimit = self.default_user_total_quota
                glimit = self.global_limit(pid, model)
                if ulimit > 0 and spent_m + price > ulimit + _EPS:
                    warn = f"\n⚠️ 你在 {disp} 的今日额度已用完，切换后暂时无法用它对话。"
                elif tlimit > 0 and total + price > tlimit + _EPS:
                    warn = "\n⚠️ 你的今日消费总额度已用完，切换后暂时无法用它对话。"
                elif glimit > 0 and gspent + price > glimit + _EPS:
                    warn = f"\n⚠️ {disp} 的全用户总限额已用完，切换后暂时无法用它对话。"
            peak_note = self.peak_row_suffix(pid, model)
            scope = "本群会话" if not event.is_private_chat() else "当前私聊会话"
            yield event.plain_result(
                f"✅ 已切换到 {idx}. {disp}，对{scope}生效。{peak_note}{warn}"
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
            bot = self._bot_id(event)
            counts, spent, total = self._personal_totals(data, ukey)
            gspent_map, gcount_map = self._pool_totals(data, bot)
            card = await self._personal_quota_card(
                event, counts, spent, gspent_map, total, self.bot_total_spent(data, bot)
            )
            if card:
                yield event.image_result(card)
                return
            lines = [f"📊 我今日剩余额度（1$≈{self.cny(1)}）："]
            rem_lines = self._remaining_lines(
                counts, spent, gspent_map, gcount_map
            )
            lines.extend(rem_lines if rem_lines else ["当前还没有开放可自选的模型。"])
            lines.extend(self._exempt_lines(event))
            lines.append(self._total_line(total))
            lines.append(self._all_users_line(self.bot_total_spent(data, bot)))
            lines.append(self._reset_line())
            lines.extend(self._peak_note_lines())
            yield event.plain_result("\n".join(lines))
            return

        # 以下均为管理员功能
        if not event.is_admin():
            yield event.plain_result("❌ 该子命令仅限管理员使用，你可以用 /quota 查自己的额度。")
            return

        data = await self._load_usage()
        records = data.get("records") if isinstance(data.get("records"), dict) else {}
        all_users = list(records.keys())
        items = self._provider_list()
        model_of = {pid: model for pid, model in items}

        if s == "all":
            bots = self._active_bots(data)
            lines = [
                f"📊 今日全量用量（{data.get('date', '')}，共 {len(all_users)} 人用过，"
                f"{len(bots)} 个 bot 有记录）："
            ]
            if not bots:
                lines.append("今日暂无用量。")
            for bot in bots:
                gspent_map, gcount_map = self._pool_totals(data, bot)
                lines.append(f"【bot {bot}】本 bot 独立总池")
                for pid in gspent_map:
                    model = model_of.get(pid, "")
                    lines.append(
                        f"- {self._display_name(pid, model)}："
                        f"单价 ${self.price_for(pid, model):.4f}/次，"
                        f"总池已花 {self.cny(self._num(gspent_map, pid))}"
                        f"/{self._fmt_limit_usd(self.global_limit(pid, model), self.cny)}"
                        f"（{gcount_map.get(pid, 0)} 次），"
                        f"每人 {self._fmt_limit_usd(self.user_model_limit(pid, model), self.cny)}"
                    )
            lines.append(
                f"每人每日总额度 "
                f"{self._fmt_limit_usd(self.default_user_total_quota, self.cny)}；"
                f"全用户总额度（本 bot 所有用户合计）"
                f"{self._fmt_limit_usd(self.all_users_total_quota, self.cny)}；"
                f"总池按 bot 分开，每模型按档位（$15→$1/$30→$2/$60→$4）"
                f"（1$≈{self.cny(1)}）"
            )
            for bot in bots:
                btotal = self.bot_total_spent(data, bot)
                lines.append(f"【bot {bot}】全用户合计已花 {self.cny(btotal)}")
            yield event.plain_result("\n".join(lines))
            return

        if s == "usage":
            if arg:
                key = arg.strip()
                matched = [
                    uk
                    for uk in all_users
                    if key in uk or uk.endswith(f":{key}")
                ]
                if not matched:
                    yield event.plain_result(f"今日没有找到用户「{key}」的用量记录。")
                    return
                lines = [f"📊 用户用量（今日，共 {len(matched)} 条匹配）："]
                for uk in matched[:20]:
                    counts, spent, total = self._personal_totals(data, uk)
                    name = self._display_name_of(data, uk)
                    detail = ", ".join(
                        f"{self._display_name(pid, model_of.get(pid, ''))}:{n}次"
                        f"·{self.cny(self._num(spent, pid))}"
                        for pid, n in counts.items()
                    )
                    lines.append(
                        f"- {name}（{uk}）：{detail or '无'}，总花 {self.cny(total)}"
                    )
                if len(matched) > 20:
                    lines.append(f"…还有 {len(matched) - 20} 条未显示，请缩小查询范围。")
                yield event.plain_result("\n".join(lines))
                return
            bots = self._active_bots(data)
            lines = [f"📊 今日用量统计（{data.get('date', '')}）："]
            if not bots:
                lines.append("今日暂无用量。")
            for bot in bots:
                gspent_map, gcount_map = self._pool_totals(data, bot)
                users_on_bot = sum(
                    1
                    for per_user in records.values()
                    if isinstance(per_user, dict) and bot in per_user
                )
                lines.append(f"【bot {bot}】{users_on_bot} 人用过")
                for pid, gspent in gspent_map.items():
                    model = model_of.get(pid, "")
                    users_used = sum(
                        1
                        for per_user in records.values()
                        if isinstance(per_user, dict)
                        and isinstance(per_user.get(bot), dict)
                        and self._num(per_user[bot].get("spent", {}), pid) > 0
                    )
                    lines.append(
                        f"- {self._display_name(pid, model)}："
                        f"总花 {self.cny(gspent if isinstance(gspent, (int, float)) else 0)}"
                        f"/{self._fmt_limit_usd(self.global_limit(pid, model), self.cny)}"
                        f"（{gcount_map.get(pid, 0)} 次，{users_used} 人用过）"
                    )
            if bots:
                lines.append(f"共 {len(all_users)} 人产生过记录。")
            yield event.plain_result("\n".join(lines))
            return

        if s == "reset":
            if arg:
                key = arg.strip()
                matched = [
                    uk
                    for uk in all_users
                    if key in uk or uk.endswith(f":{key}")
                ]
                if not matched:
                    yield event.plain_result(f"今日没有找到用户「{key}」的用量记录，无需重置。")
                    return
                for uk in matched:
                    records.pop(uk, None)
                await self._save_usage(data)
                yield event.plain_result(f"✅ 已重置 {len(matched)} 条用户记录（总池随之更新）。")
                return
            await self.put_kv_data(_USAGE_KEY, {"date": self._today(), "records": {}})
            yield event.plain_result("✅ 已清空今日全部记录（个人 + 所有 bot 的总池）。")
            return

        yield event.plain_result(self._quota_help())

    # ------------------------------------------------------------------
    # 插件页面后端（WebUI）
    # ------------------------------------------------------------------

    def _conversation_label(self, umo: str, entry: dict) -> str:
        """给对话起一个可读名字。"""
        platform = str(entry.get("platform") or "")
        if entry.get("private"):
            who = str(entry.get("sender") or entry.get("user_key") or "")
            return f"[{platform}] 私聊 · {who or umo}"
        gid = str(entry.get("group_id") or "")
        return f"[{platform}] 群聊 · {gid or umo}"

    async def _snapshot(self) -> dict:
        """汇总当前状态，供页面渲染。"""
        data = await self._load_usage()
        convs = await self._load_conversations()
        efforts = await self._load_think_efforts()
        items = self._provider_list(selectable_only=False)
        model_of = {pid: model for pid, model in items}
        catalog_items = self._provider_list(selectable_only=True)
        now = int(time.time())

        conversations = []
        for umo, entry in sorted(
            convs.items(), key=lambda kv: int(kv[1].get("ts") or 0), reverse=True
        ):
            if not isinstance(entry, dict):
                continue
            bot = str(entry.get("bot") or "")
            provider = await self._provider_of_umo(umo)
            pid = provider.meta().id if provider else ""
            model_name = (provider.meta().model or "") if provider else ""
            spent_map, count_map = self.conversation_usage(data, umo, bot or None)
            pool_spent, pool_count = self._pool_totals(data, bot) if bot else ({}, {})
            users = self.conversation_users(data, umo)
            personal_total = 0.0
            for uk in users:
                _, _, t = self._personal_totals(data, uk)
                personal_total += t
            models = []
            for mpid, spent in sorted(spent_map.items()):
                mname = model_of.get(mpid, "")
                models.append(
                    {
                        "provider_id": mpid,
                        "name": self._display_name(mpid, mname),
                        "used": int(count_map.get(mpid, 0) or 0),
                        "spent_usd": round(float(spent), 6),
                        "user_limit_usd": self.user_model_limit(mpid, mname),
                        "pool_spent_usd": round(self._num(pool_spent, mpid), 6),
                        "pool_limit_usd": self.global_limit(mpid, mname),
                        "price_usd": self.price_for(mpid, mname),
                        "think": efforts.get(mpid, self.default_think_effort),
                    }
                )
            conversations.append(
                {
                    "umo": umo,
                    "label": self._conversation_label(umo, entry),
                    "bot": bot,
                    "platform": entry.get("platform") or "",
                    "private": bool(entry.get("private")),
                    "sender": entry.get("sender") or "",
                    "users": len(users),
                    "last_seen": int(entry.get("ts") or 0),
                    "last_seen_ago": max(now - int(entry.get("ts") or 0), 0),
                    "provider_id": pid,
                    "model": self._display_name(pid, model_name) if pid else "",
                    "think": efforts.get(pid, self.default_think_effort) if pid else "",
                    "peak": self.peak_state_label(pid, model_name) if pid else "",
                    "personal_total_usd": round(personal_total, 6),
                    "models": models,
                }
            )

        catalog = []
        for pid, mname in catalog_items:
            catalog.append(
                {
                    "provider_id": pid,
                    "name": self._display_name(pid, mname),
                    "price_usd": self.price_for(pid, mname),
                    "base_price_usd": self.base_price_for(pid, mname),
                    "peak": self.has_peak_pricing(pid, mname),
                    "think": efforts.get(pid, self.default_think_effort),
                    "user_limit_usd": self.user_model_limit(pid, mname),
                    "pool_limit_usd": self.global_limit(pid, mname),
                }
            )

        return {
            "date": data.get("date", ""),
            "rate": self.rate,
            "now": now,
            "limits": {
                "user_total_usd": self.default_user_total_quota,
                "all_users_total_usd": self.all_users_total_quota,
                "user_model_default_usd": self.default_user_model_quota,
                "pool_default_usd": self.default_global_quota,
            },
            "think_levels": list(self.think_levels),
            "think_admin_only": self.think_admin_only,
            "bot_totals": {
                b: {
                    "spent_usd": self.bot_total_spent(data, b),
                    "limit_usd": self.all_users_total_quota,
                }
                for b in self._active_bots(data)
            },
            "peak_note": self._peak_summary_note(include_models=False),
            "opencode_only": bool(self.opencode_only_models and self._preset_active()),
            "conversations": conversations,
            "catalog": catalog,
        }

    async def _provider_of_umo(self, umo: str):
        try:
            return await self.context.get_using_provider_async(umo=umo)
        except Exception:
            return None

    async def web_overview(self):
        """GET：模型 / 思考强度 / 额度总览。"""
        from astrbot.api.web import json_response

        return json_response(await self._snapshot())

    async def web_reset(self):
        """POST {provider_id, umo?, scope?}：重置某模型的额度用量。

        scope: conversation（默认，只清该对话）| bot（清该 bot 上所有对话）
        """
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        provider_id = str(payload.get("provider_id") or "").strip()
        if not provider_id:
            return error_response("provider_id 不能为空", status_code=400)
        scope = str(payload.get("scope") or "conversation").strip().lower()
        umo = str(payload.get("umo") or "").strip()
        if scope not in ("conversation", "bot"):
            return error_response("scope 只能是 conversation 或 bot", status_code=400)
        if scope == "conversation" and not umo:
            return error_response("scope=conversation 时必须提供 umo", status_code=400)

        data = await self._load_usage()
        if scope == "conversation":
            bot = None
            convs = await self._load_conversations()
            entry = convs.get(umo)
            if isinstance(entry, dict):
                bot = str(entry.get("bot") or "") or None
            users, buckets = self.reset_model_usage(
                data, provider_id, bot=bot, umo=umo
            )
        else:
            users, buckets = self.reset_model_usage(data, provider_id)
        await self._save_usage(data)
        logger.info(
            f"model_quota: 页面重置 {provider_id}（scope={scope}），"
            f"涉及 {users} 人 / {buckets} 个桶"
        )
        return json_response(
            {"reset": provider_id, "scope": scope, "users": users, "buckets": buckets}
        )

    async def web_set_think(self):
        """POST {provider_id, level}：设置某模型的思考强度。"""
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        provider_id = str(payload.get("provider_id") or "").strip()
        level = str(payload.get("level") or "").strip().lower()
        if not provider_id:
            return error_response("provider_id 不能为空", status_code=400)
        if level and level not in self.think_levels and level != "reset":
            return error_response(f"不支持的级别：{level}", status_code=400)
        efforts = await self._load_think_efforts()
        if level in ("", "reset"):
            efforts.pop(provider_id, None)
        else:
            efforts[provider_id] = level
        await self._save_think_efforts()
        for provider in self._all_providers():
            try:
                if provider.meta().id == provider_id:
                    await self._apply_think_effort(provider, provider_id)
            except Exception:  # noqa: BLE001
                continue
        return json_response({"provider_id": provider_id, "level": level})

    async def terminate(self):
        """卸载时无需清理外部资源。"""
