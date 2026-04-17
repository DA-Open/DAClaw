"""
Shared platform registry for DAClaw Agent.

Single source of truth for platform metadata consumed by both
skills_config (label display) and tools_config (default toolset
resolution).  Import ``PLATFORMS`` from here instead of maintaining
duplicate dicts in each module.
"""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    ("cli",            PlatformInfo(label="🖥️  CLI",            default_toolset="daclaw-cli")),
    ("telegram",       PlatformInfo(label="📱 Telegram",        default_toolset="daclaw-telegram")),
    ("discord",        PlatformInfo(label="💬 Discord",         default_toolset="daclaw-discord")),
    ("slack",          PlatformInfo(label="💼 Slack",           default_toolset="daclaw-slack")),
    ("whatsapp",       PlatformInfo(label="📱 WhatsApp",        default_toolset="daclaw-whatsapp")),
    ("signal",         PlatformInfo(label="📡 Signal",          default_toolset="daclaw-signal")),
    ("bluebubbles",    PlatformInfo(label="💙 BlueBubbles",     default_toolset="daclaw-bluebubbles")),
    ("email",          PlatformInfo(label="📧 Email",           default_toolset="daclaw-email")),
    ("homeassistant",  PlatformInfo(label="🏠 Home Assistant",  default_toolset="daclaw-homeassistant")),
    ("mattermost",     PlatformInfo(label="💬 Mattermost",      default_toolset="daclaw-mattermost")),
    ("matrix",         PlatformInfo(label="💬 Matrix",          default_toolset="daclaw-matrix")),
    ("dingtalk",       PlatformInfo(label="💬 DingTalk",        default_toolset="daclaw-dingtalk")),
    ("feishu",         PlatformInfo(label="🪽 Feishu",          default_toolset="daclaw-feishu")),
    ("wecom",          PlatformInfo(label="💬 WeCom",           default_toolset="daclaw-wecom")),
    ("wecom_callback", PlatformInfo(label="💬 WeCom Callback",  default_toolset="daclaw-wecom-callback")),
    ("weixin",         PlatformInfo(label="💬 Weixin",          default_toolset="daclaw-weixin")),
    ("qqbot",          PlatformInfo(label="💬 QQBot",           default_toolset="daclaw-qqbot")),
    ("webhook",        PlatformInfo(label="🔗 Webhook",         default_toolset="daclaw-webhook")),
    ("api_server",     PlatformInfo(label="🌐 API Server",      default_toolset="daclaw-api-server")),
])


def platform_label(key: str, default: str = "") -> str:
    """Return the display label for a platform key, or *default*."""
    info = PLATFORMS.get(key)
    return info.label if info is not None else default
