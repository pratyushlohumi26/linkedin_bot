from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Thread, current_thread

import pytest

from telegram_bot.drafts import DraftConflict, DraftStore


def test_draft_persists_and_checks_owner_and_revision(tmp_path):
    path = str(tmp_path / "drafts.sqlite3")
    store = DraftStore(path)
    draft = store.create(10, 20, "linkedin")
    updated = store.update(draft, status="review", changes={"post_text": "Approved text"})
    assert DraftStore(path).get(draft.id, 10, 20) == updated
    with pytest.raises(DraftConflict):
        store.get(draft.id, 10, 21)
    with pytest.raises(DraftConflict):
        store.get(draft.id, 11, 20)
    with pytest.raises(DraftConflict):
        store.update(draft, status="publishing")


def test_only_one_concurrent_publish_claim_wins(tmp_path):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    draft = store.create(1, 2, "linkedin")

    def claim(_):
        try:
            store.update(draft, status="publishing")
            return True
        except DraftConflict:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(claim, range(4))) == 1


def test_new_draft_cancels_old_work_but_not_inflight_publication(tmp_path):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    old = store.create(1, 2, "linkedin")
    new = store.create(1, 2, "linkedin")
    assert store.get(old.id, 1, 2).status == "cancelled"
    with pytest.raises(DraftConflict):
        store.update(old, status="review")
    store.update(new, status="publishing")
    with pytest.raises(DraftConflict):
        store.create(1, 2, "both")


@pytest.mark.parametrize("platform", ["linkedin", "twitter"])
def test_restart_never_retries_ambiguous_publication(tmp_path, platform):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    draft = store.create(1, 2, "both")
    store.update(
        draft,
        status="publishing",
        changes={platform + "_status": "publishing", "post_text": "text"},
    )
    assert store.recover_interrupted() == 1
    recovered = store.get(draft.id, 1, 2)
    assert recovered.status == "uncertain"
    assert recovered.data[platform + "_status"] == "uncertain"
    assert store.recover_interrupted() == 0


def test_restart_preserves_published_id_and_generation_budget(tmp_path):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    image_draft = store.create(1, 2, "linkedin")
    store.update(
        image_draft,
        status="generating_image",
        changes={"post_text": "text", "image_generations": 2},
    )
    published = store.create(3, 4, "both")
    store.update(
        published,
        status="publishing",
        changes={"linkedin_status": "published", "linkedin_urn": "urn:li:share:42"},
    )
    store.recover_interrupted()
    assert store.get(image_draft.id, 1, 2).data["image_generations"] == 2
    assert store.get(image_draft.id, 1, 2).status == "review"
    assert store.get(published.id, 3, 4).status == "partial"
    assert store.get(published.id, 3, 4).data["linkedin_urn"] == "urn:li:share:42"


def test_expiration_never_removes_active_job(tmp_path):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    expired = store.create(1, 2, "linkedin")
    busy = store.create(3, 4, "linkedin")
    store.update(busy, status="generating_image")
    with store.connection() as db:
        db.execute("UPDATE drafts SET updated_at=0")
    assert store.expire(7) == [expired.id]
    assert store.get(busy.id, 3, 4).status == "generating_image"


def test_late_comment_status_does_not_hide_newer_draft(tmp_path):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    old = store.create(1, 2, "linkedin")
    old = store.update(old, status="published")
    new = store.create(1, 2, "linkedin")
    store.update(old, changes={"first_comment_status": "posted"})
    assert store.latest(1, 2).id == new.id


def test_update_returns_its_own_snapshot_when_cancel_wins_after_commit(tmp_path):
    committed, release = Event(), Event()

    class InterleavedStore(DraftStore):
        pause = False

        @contextmanager
        def connection(self):
            with super().connection() as db:
                yield db
            if self.pause and current_thread().name == "snapshot-worker":
                self.pause = False
                committed.set()
                assert release.wait(5)

    store = InterleavedStore(str(tmp_path / "drafts.sqlite3"))
    draft = store.create(1, 2, "linkedin")
    store.pause = True
    snapshots = []
    worker = Thread(
        target=lambda: snapshots.append(store.update(draft, status="generating_image")),
        name="snapshot-worker",
    )
    worker.start()
    try:
        assert committed.wait(5)
        busy = store.get(draft.id, 1, 2)
        store.update(busy, status="cancelled")
    finally:
        release.set()
        worker.join(5)
    assert snapshots[0].status == "generating_image"
    with pytest.raises(DraftConflict):
        store.update(snapshots[0], status="review")
    assert store.get(draft.id, 1, 2).status == "cancelled"


@pytest.mark.parametrize("feed", ["linkedin", "twitter", "both"])
def test_restart_recognizes_all_destinations_already_published(tmp_path, feed):
    store = DraftStore(str(tmp_path / "drafts.sqlite3"))
    draft = store.create(1, 2, feed)
    platforms = ["linkedin", "twitter"] if feed == "both" else [feed]
    store.update(
        draft, status="publishing", changes={p + "_status": "published" for p in platforms}
    )
    store.recover_interrupted()
    assert store.get(draft.id, 1, 2).status == "published"
