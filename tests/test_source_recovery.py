"""Real source workflow/SQLite/controller tests with HTTP and Telegram boundaries replaced."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Thread

import pytest
from telebot.types import Message
from test_scraper import CHALLENGE, URL, HTTPBoundary
from test_summarizer import BRIEF, _brief_generator, httpx
from test_workflow import TelegramBoundary, buttons, callback_for, with_image
from test_workflow import setup_flow as setup_flow

from telegram_bot.drafts import DraftConflict
from telegram_bot.handlers import DraftBotController
from telegram_bot.scraper import ArticleScraper


def source_flow(flow, boundary=None):
    flow.scraper = ArticleScraper(
        replace(flow.config.scraper, cache_ttl_seconds=0), fetcher=boundary or HTTPBoundary()
    )
    return flow.store.create(10, 20, "linkedin")


def message(text, message_id=5, user=20):
    return Message.de_json(
        {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": 10, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Tester"},
            "text": text,
        }
    )


def test_blocked_fetch_recovers_with_multiple_paste_chunks_without_generation(setup_flow):
    flow, api = setup_flow
    draft = source_flow(flow, HTTPBoundary(CHALLENGE, 403))
    blocked = flow.scrape(draft, URL)
    assert blocked.status == "source_recovery"
    assert blocked.data["source_metadata"]["status"] == "blocked"
    assert not blocked.data["article_text"]
    with pytest.raises(ValueError, match="not retryable"):
        flow.scrape(blocked, URL)
    draft = flow.begin_source_text(blocked)
    first = flow.append_source_text(draft, "First part of the original article.", chunk_id=5)
    second = flow.append_source_text(first, "Second part with additional evidence.", chunk_id=6)
    assert second.status == "awaiting_source_text"
    assert not second.data.get("article_text")
    result = flow.finish_source_text(second)
    assert result.status == "source_review"
    assert (
        result.data["article_text"]
        == "First part of the original article.\n\nSecond part with additional evidence."
    )
    assert result.data["source_url"] == URL
    assert result.data["source_metadata"]["method"].startswith("user_supplied:")
    assert result.data["source_hash"]
    assert api.posts == [] and api.generations == 0


def test_source_replacement_invalidates_images_variants_and_approval(setup_flow):
    flow, api = setup_flow
    image = with_image(flow)
    image = flow.store.update(image, changes={"approved_revision": image.revision})
    draft = flow.replace_source(image)
    for key in (
        "article_text",
        "variants",
        "post_text",
        "brief",
        "image_path",
        "image_asset_urn",
        "approved_revision",
    ):
        assert draft.data[key] is None
    assert draft.data["image_generations"] == 1
    with pytest.raises(DraftConflict):
        flow.publish(image)
    assert not api.posts


def test_concurrent_paste_chunks_merge_in_message_order_and_deduplicate(setup_flow):
    flow, _ = setup_flow
    draft = flow.begin_source_text(source_flow(flow))
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(flow.append_source_text, draft, "Later message.", chunk_id=10)
        two = executor.submit(flow.append_source_text, draft, "Earlier message.", chunk_id=9)
        one.result()
        two.result()
    current = flow.store.get(draft.id, 10, 20)
    duplicate = flow.append_source_text(current, "Later message.", chunk_id=10)
    assert duplicate.revision == current.revision
    result = flow.finish_source_text(current)
    assert result.data["article_text"] == "Earlier message.\n\nLater message."


def test_cancel_and_restarted_collection_reject_old_chunks(setup_flow):
    flow, _ = setup_flow
    old = flow.begin_source_text(source_flow(flow))
    cleared = flow.replace_source(old)
    new = flow.begin_source_text(cleared)
    with pytest.raises(DraftConflict):
        flow.append_source_text(old, "Stale text.", chunk_id=10)
    assert flow.store.get(new.id, 10, 20).data["source_chunks"] == []
    flow.cancel(new)
    with pytest.raises(DraftConflict):
        flow.append_source_text(new, "Late text.", chunk_id=11)


def test_limits_and_empty_done_do_not_discard_saved_chunks(setup_flow):
    flow, _ = setup_flow
    flow.config = replace(flow.config, scraper=replace(flow.config.scraper, max_text_chars=1000))
    draft = flow.begin_source_text(source_flow(flow))
    with pytest.raises(ValueError):
        flow.finish_source_text(draft)
    draft = flow.append_source_text(draft, "a" * 600, chunk_id=1)
    with pytest.raises(ValueError):
        flow.append_source_text(draft, "b" * 600, chunk_id=2)
    assert len(flow.store.get(draft.id, 10, 20).data["source_chunks"]) == 1


def test_pasted_source_and_interrupted_file_read_survive_restart(setup_flow):
    flow, _ = setup_flow
    draft = flow.begin_source_text(source_flow(flow))
    draft = flow.append_source_text(draft, "A persisted article part.", chunk_id=10)
    draft = flow.store.update(draft, status="reading_source")
    assert flow.store.recover_interrupted() == 1
    restored = flow.store.get(draft.id, 10, 20)
    assert restored.status == "awaiting_source_text"
    assert flow.finish_source_text(restored).data["article_text"] == "A persisted article part."


def test_interrupted_scrape_offers_explicit_retry_not_automatic_fetch(setup_flow):
    flow, _ = setup_flow
    draft = source_flow(flow)
    draft = flow.store.update(draft, status="scraping", changes={"source_url": URL})
    flow.store.recover_interrupted()
    restored = flow.store.get(draft.id, 10, 20)
    assert restored.status == "source_recovery"
    assert restored.data["source_metadata"]["retryable"]
    assert not flow.scraper.fetcher.calls


def test_telemetry_failure_does_not_destroy_successful_source(setup_flow, monkeypatch):
    flow, _ = setup_flow
    draft = source_flow(flow)

    def unavailable(*args, **kwargs):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(flow.telemetry, "record", unavailable)
    result = flow.scrape(draft, URL)
    assert result.status == "source_review" and result.data["article_text"]


def test_cancel_during_fetch_cannot_restore_source(setup_flow):
    flow, _ = setup_flow
    started, release = Event(), Event()

    class WaitingHTTP(HTTPBoundary):
        def fetch(self, url, *, deadline=None, before_request=None):
            if url == URL:
                started.set()
                assert release.wait(5)
            return super().fetch(url, deadline=deadline, before_request=before_request)

    draft = source_flow(flow, WaitingHTTP())
    failures = []

    def work():
        try:
            flow.scrape(draft, URL)
        except DraftConflict as exc:
            failures.append(exc)

    thread = Thread(target=work)
    thread.start()
    assert started.wait(5)
    flow.cancel(flow.store.get(draft.id, 10, 20))
    release.set()
    thread.join(5)
    assert not thread.is_alive() and failures
    assert flow.store.get(draft.id, 10, 20).status == "cancelled"


def test_real_controller_blocked_paste_done_source_download_and_stale_controls(setup_flow):
    flow, api = setup_flow
    bot = TelegramBoundary()
    controller = DraftBotController(bot, flow.config, workflow=flow)
    try:
        draft = flow.scrape(source_flow(flow, HTTPBoundary(CHALLENGE, 403)), URL)
        controller.present(draft)
        assert not any("Retry fetching" in b.text for b in buttons(bot))
        controller.callback(callback_for(draft, "paste"))
        collecting = flow.store.get(draft.id, 10, 20)
        bot.process_new_messages(
            [message("An article describing the new source and its evidence.")]
        )
        saved = flow.store.get(draft.id, 10, 20)
        controller.callback(callback_for(collecting, "source_done"))
        assert flow.store.get(draft.id, 10, 20).status == "awaiting_source_text"
        controller.callback(callback_for(saved, "source_done"))
        reviewed = flow.store.get(draft.id, 10, 20)
        assert reviewed.status == "source_review"
        controller.callback(callback_for(reviewed, "source_full"))
        assert any(
            kind == "document" and content == reviewed.data["article_text"].encode()
            for kind, content, _ in bot.sent
        )
        assert api.posts == [] and api.generations == 0
    finally:
        controller.close()


def test_other_user_cannot_finish_or_read_source(setup_flow):
    flow, _ = setup_flow
    bot = TelegramBoundary()
    controller = DraftBotController(bot, flow.config, workflow=flow)
    try:
        draft = flow.begin_source_text(source_flow(flow))
        draft = flow.append_source_text(draft, "Private draft article.", chunk_id=1)
        controller.callback(callback_for(draft, "source_done", user=99))
        assert flow.store.get(draft.id, 10, 20).status == "awaiting_source_text"
        assert not any(kind == "document" for kind, _, _ in bot.sent)
    finally:
        controller.close()


@pytest.mark.parametrize("source", ["url", "paste"])
def test_new_source_can_create_image_idea_without_custom_instructions(
    setup_flow, monkeypatch, source
):
    flow, api = setup_flow
    draft = source_flow(flow)
    if source == "url":
        draft = flow.scrape(draft, URL)
    else:
        draft = flow.begin_source_text(draft)
        draft = flow.append_source_text(
            draft, "An article about local inference and its deployment trade-offs.", chunk_id=1
        )
        draft = flow.finish_source_text(draft)
    draft = flow.select(flow.generate_variants(draft), "A")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "text-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(BRIEF)},
                    }
                ],
            },
        )

    generator = _brief_generator(monkeypatch, provider)
    flow.generator = generator
    try:
        result = flow.make_brief(draft)
        assert result.data["brief"] == BRIEF
        assert len(calls) == 1 and not api.posts
    finally:
        generator._client.close()


@pytest.mark.parametrize("feed", ["linkedin", "twitter", "both"])
def test_restart_during_initial_generation_retains_reviewable_source(setup_flow, feed):
    flow, api = setup_flow
    draft = flow.store.create(10, 20, feed)
    draft = flow.begin_source_text(draft)
    draft = flow.append_source_text(
        draft, "An article that must remain available after restart.", chunk_id=1
    )
    draft = flow.finish_source_text(draft)
    original_hash = draft.data["source_hash"]
    draft = flow.store.update(draft, status="generating_text")
    flow.store.recover_interrupted()
    restored = flow.store.get(draft.id, 10, 20)
    assert restored.status == "source_review"
    assert restored.data["source_hash"] == original_hash
    assert not api.posts and api.generations == 0
    bot = TelegramBoundary()
    controller = DraftBotController(bot, flow.config, workflow=flow)
    try:
        controller.present(restored)
        controller.callback(callback_for(restored, "source_full"))
        assert any(kind == "document" for kind, _, _ in bot.sent)
    finally:
        controller.close()
