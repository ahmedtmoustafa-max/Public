from __future__ import annotations

from phone_agent.playbooks import PlaybookStore, parse_key_script


def test_parse_key_script_splits_levels_and_strips_junk():
    assert parse_key_script("1,w3,0") == ["1", "w3", "0"]
    assert parse_key_script(" 1 , 2 ") == ["1", "2"]
    assert parse_key_script("*1,#") == ["*1", "#"]
    assert parse_key_script("press 1, then 2") == ["1", "2"]
    assert parse_key_script("") == []


def test_save_and_read_back(tmp_path):
    store = PlaybookStore(tmp_path)
    store.learn(
        label="Test Clinic",
        number="+15551234567",
        keys="1,0",
        goal="book an appointment",
        call_id="abc123",
    )
    books = store.all()
    assert len(books) == 1
    assert books[0].id == "test-clinic"
    assert books[0].keys == "1,0"
    assert books[0].source_calls == ["abc123"]


def test_lookup_by_number_ignores_formatting(tmp_path):
    store = PlaybookStore(tmp_path)
    store.learn(label="Clinic", number="+1 (555) 123-4567", keys="1",
                goal="", call_id="x")
    assert store.find_for_number("+15551234567") is not None
    assert store.find_for_number("+15559999999") is None


def test_relearning_the_same_keys_just_records_the_call(tmp_path):
    store = PlaybookStore(tmp_path)
    store.learn(label="Clinic", number="+15551234567", keys="1,0", goal="", call_id="one")
    store.learn(label="Clinic", number="+15551234567", keys="1,0", goal="", call_id="two")
    books = store.all()
    assert len(books) == 1
    assert books[0].source_calls == ["one", "two"]


def test_no_keys_means_nothing_is_saved(tmp_path):
    store = PlaybookStore(tmp_path)
    assert store.learn(label="X", number="+1555", keys="", goal="", call_id="x") is None
    assert store.all() == []


def test_unreadable_playbook_is_skipped_not_fatal(tmp_path):
    (tmp_path / "broken.yaml").write_text("{{{ not yaml")
    (tmp_path / "good.yaml").write_text("id: good\nlabel: Good\nkeys: '1'\n")
    books = PlaybookStore(tmp_path).all()
    assert [b.id for b in books] == ["good"]
