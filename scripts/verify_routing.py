"""Verify Telegram room routing — make sure every channel resolves to the
right destination and nothing can shuffle between rooms.

Read-only by default: prints the full routing table (source -> channel ->
chat + forum topic) and flags any gaps (channels with no destination, topics
with no feeder, sources that silently fall through to the default channel).

With --probe it sends ONE labeled test message to each configured topic so you
can eyeball that topic id N really is the room you think it is. This posts to
your live group; you'll be asked to confirm first.

Usage:
    python -m scripts.verify_routing            # audit only (no messages)
    python -m scripts.verify_routing --probe     # also send labeled probes
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a script (python scripts/verify_routing.py) or a module.
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import requests  # noqa: E402

import config_loader  # noqa: E402
from alert_policy import assign_channel  # noqa: E402

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled_source_names(sources_cfg: dict) -> list[str]:
    """Reconstruct the source names build_sources() would create, for enabled groups.

    Mirrors sources/__init__.py naming so the audit reflects what actually runs.
    """
    names: list[str] = []

    def on(key: str) -> dict:
        v = sources_cfg.get(key, {})
        return v if isinstance(v, dict) and v.get("enabled") else {}

    for f in on("rss").get("feeds", []):
        names.append(f"rss:{f.get('name')}")
    for q in on("news_search").get("queries", []):
        names.append(f"news_search:{q.get('name') or q.get('query') if isinstance(q, dict) else q}")
    if on("polymarket"):
        names.append("polymarket:markets")
    if on("kalshi"):
        names.append("kalshi:markets")
    if on("market_news"):
        names.append("marketnews:hot")
    if on("usaspending"):
        names.append("usaspending:contracts")
    if on("ratings"):
        names.append("ratings:fmp")
    if on("sec_stakes"):
        names.append("sec:13d")
    for q in on("institutions_news").get("queries", []):
        names.append(f"instnews:{q.get('name') if isinstance(q, dict) else q}")
    if on("gdelt"):
        names.append("gdelt:doc")
    if on("newsapi"):
        names.append("newsapi:everything")
    for c in on("youtube").get("channels", []):
        names.append(f"youtube:{c.get('name') if isinstance(c, dict) else c}")
    for a in on("reddit").get("feeds", []):
        names.append(f"reddit:{a.get('name') if isinstance(a, dict) else a}")
    for a in on("truthsocial").get("accounts", []):
        names.append(f"truthsocial:{a}")
    return names


def audit() -> tuple[dict, dict, dict, dict]:
    settings = config_loader.load_settings()
    channels = config_loader.load_channels()
    topics = config_loader.load_topics()
    sources_cfg = config_loader.load_sources()
    chats = settings.channel_chats
    default_chat = settings.telegram_chat_id
    default_channel = channels.get("default_channel", "default")

    routes = channels.get("routes", {})
    routed_channels = set(routes.values()) | {default_channel}

    print("=" * 72)
    print("ROOM ROUTING AUDIT")
    print("=" * 72)
    print(f"Default chat id (TELEGRAM_CHAT_ID): {default_chat}")
    print(f"Default channel (catch-all): {default_channel!r}")
    print(f"Per-channel chats (TELEGRAM_CHAT_*): {chats or '(none — all share one group)'}")
    print()

    # --- destination per channel ---------------------------------------- #
    print(f"{'CHANNEL':14} {'TOPIC':>6}  {'CHAT':>16}  DESTINATION")
    print("-" * 72)
    for chan in sorted(routed_channels):
        topic = topics.get(chan)
        chat = chats.get(chan, default_chat)
        if topic:
            dest = f"topic #{topic} in main group"
        elif chan in chats:
            dest = "separate chat"
        else:
            dest = "⚠️  GENERAL thread (no topic, no chat) — will mix with other rooms!"
        print(f"{chan:14} {str(topic or '-'):>6}  {str(chat):>16}  {dest}")
    print()

    # --- per-source resolution ------------------------------------------ #
    names = _enabled_source_names(sources_cfg)
    print(f"{'CHANNEL':14} {'TOPIC':>6}  SOURCE  (enabled sources only)")
    print("-" * 72)
    fell_to_default = []
    for n in names:
        chan = assign_channel(n, channels)
        topic = topics.get(chan)
        # A source falls through to the default only if no route prefix matched.
        matched = any(n.lower().startswith(k.lower()) for k in routes)
        flag = ""
        if not matched:
            flag = "  ⟵ no route matched, using DEFAULT"
            fell_to_default.append(n)
        print(f"{chan:14} {str(topic or '-'):>6}  {n}{flag}")
    print()

    # --- gaps ----------------------------------------------------------- #
    print("=" * 72)
    print("GAPS / RISKS")
    print("=" * 72)
    no_dest = sorted(c for c in routed_channels
                     if c not in topics and c not in chats)
    orphan_topics = sorted(set(topics) - routed_channels)
    if no_dest:
        print(f"⚠️  Channels with NO dedicated room (fall to General/default chat): {no_dest}")
    if fell_to_default:
        print(f"⚠️  Enabled sources with no explicit route (→ {default_channel!r}): {fell_to_default}")
    if orphan_topics:
        print(f"ℹ️  Topics with no source feeding them (notice-only is fine): {orphan_topics}")
    if not (no_dest or fell_to_default):
        print("✅ Every enabled source maps to an explicitly-routed channel with a destination.")
    print()
    return channels, topics, chats, {"default_chat": default_chat}


def probe(channels: dict, topics: dict, chats: dict, meta: dict) -> None:
    settings = config_loader.load_settings()
    token = settings.telegram_bot_token
    if not token:
        print("No TELEGRAM_BOT_TOKEN — cannot send probes.")
        return
    default_chat = meta["default_chat"]
    print("Sending one labeled probe to each configured topic…")
    for chan, topic in sorted(topics.items(), key=lambda kv: kv[1]):
        chat = chats.get(chan, default_chat)
        text = (f"🧭 ROUTING PROBE\nThis message was sent to channel <b>{chan}</b> "
                f"(topic id <code>{topic}</code>).\nIf you are NOT in the "
                f"<b>{chan}</b> room, topics.json is mismatched.")
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
                   "message_thread_id": topic, "disable_web_page_preview": True}
        try:
            r = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=15)
            ok = r.status_code == 200
            print(f"  {chan:14} topic #{topic} -> {'sent ✅' if ok else f'FAILED {r.status_code}: {r.text[:120]}'}")
        except requests.RequestException as exc:
            print(f"  {chan:14} topic #{topic} -> error: {exc}")
    print("\nNow open each room in Telegram and confirm the probe text matches the room name.")


if __name__ == "__main__":
    channels, topics, chats, meta = audit()
    if "--probe" in sys.argv:
        probe(channels, topics, chats, meta)
    else:
        print("Run with --probe to send one labeled test message per room (posts to your live group).")
