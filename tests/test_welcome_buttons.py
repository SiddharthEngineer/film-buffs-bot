"""#welcome page buttons: Letterboxd link/sync, Weekly Ping (no group buttons)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import setup_server
from filmbuffs import welcome_ui
from filmbuffs.welcome_ui import LinkModal, SyncModal, WelcomeButton


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ server.yaml
def _page(cfg):
    return next(m for m in cfg["messages"] if m["key"] == "welcome_rules")


def test_welcome_page_has_buttons_and_notifications():
    page = _page(setup_server.load_config("server.yaml"))
    assert all(isinstance(t, str) for t in page["sections"])
    actions = [b["action"] for row in page["_page_buttons"].values() for b in row]
    assert actions == ["letterboxd_link", "letterboxd_sync", "weekly_pings"]
    labels = [b["label"] for row in page["_page_buttons"].values() for b in row]
    assert labels == ["Link Letterboxd", "Sync History", "Weekly Ping"]
    notif = next(t for t in page["sections"] if t.startswith("## 🔔 Notifications"))
    for bit in ("Only @mentions", "Thursday morning", "Sunday morning", "Early Monday morning",
                "Notification Settings", "All Messages"):
        assert bit in notif
    assert "Group Ping" not in "\n".join(page["sections"]) and "follow" not in notif
    assert setup_server.page_components(page["sections"], page["_page_buttons"]) <= 40


def _cfg_with(tmp_path, old, new):
    src = Path("server.yaml").read_text()
    assert old in src
    p = tmp_path / "s.yaml"
    p.write_text(src.replace(old, new, 1))
    return p


@pytest.mark.parametrize("old,new,err", [
    ("action: letterboxd_link,", "action: nope,", "unknown button action"),
    ('style: primary}', 'style: purple}', "button style"),
    ('label: "Weekly Ping"', 'label: ""', "labels are 1-80"),
])
def test_page_button_errors(tmp_path, old, new, err):
    with pytest.raises(setup_server.ConfigError, match=err):
        setup_server.load_config(_cfg_with(tmp_path, old, new))


def test_build_page_puts_a_row_under_the_section():
    buttons = {1: [{"action": "weekly_pings", "label": "Weekly Ping", "emoji": "🔔"}]}
    view = setup_server.build_page(["# A", "## B", "## C"], 1, buttons)
    kinds = [type(c).__name__ for c in view.children[0].children]
    assert kinds == ["TextDisplay", "Separator", "TextDisplay", "ActionRow", "Separator",
                     "TextDisplay"]
    row = view.children[0].children[3]
    assert [b.custom_id for b in row.children] == ["welcome:weekly_pings"]


# ------------------------------------------------------------------ clicks
class Resp:
    def __init__(self):
        self.sent, self.modal = [], None

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))

    async def send_modal(self, modal):
        self.modal = modal


def _interaction(*, link=None, roles=(), groups=None, syncing=()):
    cog = SimpleNamespace(_syncing=set(syncing), linked=[], synced=[])

    async def link_account(inter, name):
        cog.linked.append(name)

    async def sync_export(inter, f):
        cog.synced.append(f)
    cog.link_account, cog.sync_export = link_account, sync_export

    async def get_link(uid):
        return link
    client = SimpleNamespace(
        db=SimpleNamespace(get_link=get_link), get_cog=lambda name: cog,
        config=SimpleNamespace(roles={"weekly_ping": 50}, groups=groups or {}))
    user = SimpleNamespace(id=7, roles=[SimpleNamespace(id=r) for r in roles])
    return SimpleNamespace(client=client, user=user, response=Resp()), cog


def test_link_opens_a_prefilled_modal_and_submits_to_the_cog():
    inter, cog = _interaction(link={"username": "annie"})
    run(WelcomeButton("letterboxd_link").callback(inter))
    modal = inter.response.modal
    assert isinstance(modal, LinkModal) and modal.username.default == "annie"
    modal.username._value = "  bob  "
    run(modal.on_submit(inter))
    assert cog.linked == ["bob"]


def test_sync_needs_a_link_first_then_opens_upload_modal():
    inter, _ = _interaction(link=None)
    run(WelcomeButton("letterboxd_sync").callback(inter))
    assert inter.response.modal is None and "Link your Letterboxd account first" in inter.response.sent[0][0]

    inter, cog = _interaction(link={"username": "annie"})
    run(WelcomeButton("letterboxd_sync").callback(inter))
    modal = inter.response.modal
    assert isinstance(modal, SyncModal)
    assert modal.to_dict()["components"][0]["component"]["type"] == 19   # File Upload in a Label
    modal.upload._values = ["zip"]
    run(modal.on_submit(inter))
    assert cog.synced == ["zip"]


def test_sync_refuses_while_one_is_running():
    inter, _ = _interaction(link={"username": "annie"}, syncing={7})
    run(WelcomeButton("letterboxd_sync").callback(inter))
    assert inter.response.modal is None and "still running" in inter.response.sent[0][0]


def test_weekly_ping_button(monkeypatch):
    toggled = []

    async def toggle(inter, rid, why):
        toggled.append(rid)
    monkeypatch.setattr("filmbuffs.vote_ui.toggle_role", toggle)
    inter, _ = _interaction()
    run(WelcomeButton("weekly_pings").callback(inter))
    assert toggled == [50]


def test_unknown_action_is_harmless():
    inter, _ = _interaction()
    run(WelcomeButton("gone").callback(inter))
    assert "doesn't do anything" in inter.response.sent[0][0]
    assert set(welcome_ui.WELCOME_ACTIONS) == {"letterboxd_link", "letterboxd_sync",
                                               "weekly_pings"}
    run(WelcomeButton("group_pings").callback(inter))      # retired: never shows group names
    assert "doesn't do anything" in inter.response.sent[1][0]
