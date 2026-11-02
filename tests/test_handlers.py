"""Which updates reach handle_url and handle_help. Uses real PTB objects."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, MessageEntity, Update

import main

_handlers = main._handlers()


def _update(chat_type, text=None, caption=None, entities=(), caption_entities=(), edited=False):
    chat = Chat(id=-100 if chat_type != "private" else 1, type=chat_type)
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
    )
    # CommandHandler reads the bot username, for commands like /help@bot.
    message.set_bot(SimpleNamespace(username="tg_yt_dlp_bot"))
    field = "channel_post" if chat_type == "channel" else "message"
    if edited:
        field = "edited_" + field
    return Update(update_id=1, **{field: message})


def _callbacks(update):
    return {h.callback.__name__ for h in _handlers if h.check_update(update)}


def _url(text, offset):
    return (MessageEntity(MessageEntity.URL, offset, len(text) - offset),)


@pytest.mark.parametrize("chat_type", ["channel", "group", "supergroup"])
def test_link_post_is_handled(chat_type):
    text = "look https://youtu.be/x"
    assert _callbacks(_update(chat_type, text=text, entities=_url(text, 5))) == {"handle_url"}


def test_link_in_caption_is_handled():
    caption = "https://youtu.be/x"
    update = _update("channel", caption=caption, caption_entities=_url(caption, 0))
    assert _callbacks(update) == {"handle_url"}


def test_hidden_link_is_handled_and_found():
    link = MessageEntity(MessageEntity.TEXT_LINK, 0, 5, url="https://youtu.be/x")
    update = _update("channel", text="video", entities=(link,))
    assert _callbacks(update) == {"handle_url"}
    assert main._find_url(update.effective_message, "video") == "https://youtu.be/x"


@pytest.mark.parametrize("chat_type", ["channel", "supergroup"])
def test_post_without_link_is_ignored(chat_type):
    assert _callbacks(_update(chat_type, text="hello everyone")) == set()


def test_edited_post_is_ignored():
    text = "https://youtu.be/x"
    assert _callbacks(_update("channel", text=text, entities=_url(text, 0), edited=True)) == set()


def test_help_only_in_private_chats():
    command = (MessageEntity(MessageEntity.BOT_COMMAND, 0, 5),)
    assert _callbacks(_update("private", text="/help", entities=command)) == {"handle_help"}
    assert _callbacks(_update("supergroup", text="/help", entities=command)) == set()


def test_private_text_without_link_still_gets_an_answer():
    assert _callbacks(_update("private", text="hello")) == {"handle_url"}
