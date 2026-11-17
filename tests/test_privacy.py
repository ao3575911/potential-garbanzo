from telegram import MessageEntity

from circuit_bot.privacy import has_link, redact


def test_redacts_common_identifiers_and_sender_name():
    text = "I am Alice Smith. Contact @alice_user, alice@example.com, +61 412 345 678 or https://example.com. The transaction failed."
    result = redact(text, ("Alice Smith",))
    for identifier in ("Alice", "@alice_user", "example.com", "412"):
        assert identifier not in result
    assert "The transaction failed" in result


def test_hidden_links_and_utf16_entities_removed():
    text = "😀 contact me about this situation"
    entity = MessageEntity(MessageEntity.TEXT_LINK, offset=3, length=10, url="https://private.example")
    assert redact(text, entities=[entity]) == "😀 [removed] about this situation"


def test_plain_text_remains_plain_without_html_injection():
    assert redact("<b>Nothing agreed was delivered.</b>") == "<b>Nothing agreed was delivered.</b>"


def test_invisible_characters_cannot_hide_a_username():
    assert "alice" not in redact("Message @a\u200blice123 for more details.")


def test_links_detected_in_captions_and_text_link_entities():
    assert has_link("example.com/path")
    assert has_link("hidden", [MessageEntity(MessageEntity.TEXT_LINK, 0, 6, url="https://example.com")])
    assert not has_link("A normal conversation.")
