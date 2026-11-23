import json
from collections import Counter
from pathlib import Path

import pytest

from gateway import filter_orders

DATA = Path(__file__).resolve().parent.parent / "events.jsonl"


def make(index, **changes):
    event = {
        "id": f"E{index:04d}",
        "ts": "2026-11-20T10:00:00",
        "device_id": f"dv-{index:06d}",
        "ip": f"102.0.0.{index % 250}",
        "customer": f"زبون {index}",
        "item": "لاتيه",
        "size": "M",
        "qty": 1,
        "note": "",
        "request_id": f"rq-{index:08d}",
    }
    event.update(changes)
    return event


def at(seconds):
    minutes, secs = divmod(seconds, 60)
    return f"2026-11-20T10:{minutes:02d}:{secs:02d}"


def reasons(events):
    return [item["reason"] for item in filter_orders(events)]


def test_clean_order_is_accepted():
    assert filter_orders([make(1)]) == [{"id": "E0001", "decision": "accept", "reason": None}]


def test_output_keeps_order_and_ids():
    events = [make(3), make(1, qty=0), make(2)]
    result = filter_orders(events)
    assert [item["id"] for item in result] == ["E0003", "E0001", "E0002"]
    assert [item["decision"] for item in result] == ["accept", "reject", "accept"]


def test_empty_input():
    assert filter_orders([]) == []


def test_same_request_id_is_duplicate_even_after_a_long_gap():
    first = make(1, request_id="rq-same")
    second = make(2, request_id="rq-same", ts=at(1800))
    assert reasons([first, second]) == [None, "duplicate"]


def test_same_content_with_new_request_id_inside_window_is_duplicate():
    first = make(1, device_id="dv-a", customer="سلمى")
    second = make(2, device_id="dv-a", customer="سلمى", ts=at(5))
    assert reasons([first, second]) == [None, "duplicate"]


def test_same_content_after_window_is_a_real_reorder():
    first = make(1, device_id="dv-a", customer="سلمى")
    second = make(2, device_id="dv-a", customer="سلمى", ts=at(240))
    assert reasons([first, second]) == [None, None]


def test_same_customer_ordering_something_different_is_not_duplicate():
    first = make(1, device_id="dv-a", customer="سلمى", item="لاتيه")
    second = make(2, device_id="dv-a", customer="سلمى", item="V60", ts=at(5))
    assert reasons([first, second]) == [None, None]


def test_device_switching_customers_quickly_is_burst():
    events = [
        make(1, device_id="dv-x", customer="أحمد", ts=at(0)),
        make(2, device_id="dv-x", customer="منى", ts=at(3)),
        make(3, device_id="dv-x", customer="هدى", ts=at(6)),
    ]
    assert reasons(events) == [None, "burst", "burst"]


def test_device_burst_forgets_after_window():
    events = [
        make(1, device_id="dv-x", customer="أحمد", ts=at(0)),
        make(2, device_id="dv-x", customer="منى", ts=at(300)),
    ]
    assert reasons(events) == [None, None]


def test_many_devices_behind_one_ip_are_not_a_burst():
    events = [make(i, ip="105.2.59.72", ts=at(i)) for i in range(1, 7)]
    assert reasons(events) == [None] * 6


@pytest.mark.parametrize(
    "note",
    [
        "check this https://free-gift.example/198208",
        "اشتراك VIP wa.me/9665259618",
        "t.me/crypto_signals_11a379",
        "قروض بدون فوائد تواصل واتساب",
        "win iphone free today",
        "qwertyuiop",
        "kkkkkkkkkkk",
        "ااااااااااا",
        "asdkjh qwe zxcv",
        "lorem ipsum dolor",
        "https://google.com/url?q=http://evil.com",
        "https://goo.gl/abc",
        "TELEGRAM.ME/x",
        "earn $500 daily",
        "50% off today",
        "واربح الان",
    ],
)
def test_spam_notes_are_rejected(note):
    assert reasons([make(1, note=note)]) == ["spam_content"]


@pytest.mark.parametrize(
    "note",
    [
        "extra hot please",
        "less ice please",
        "بدون سكر",
        "سكر قليل 🙏",
        "حليب لوز",
        "أنا في الموقع https://maps.app.goo.gl/c964d3d449",
        "https://www.google.com/maps/place/cafe",
        "sugar free please",
        "في خصم للطلبة؟",
        "شكرااااا",
        "هههههههههه",
        "call 01000000000",
        "brrr cold today",
        None,
    ],
)
def test_normal_notes_are_accepted(note):
    assert reasons([make(1, note=note)]) == [None]


@pytest.mark.parametrize("qty", [1, 2, 3, 10, 13, 20])
def test_reasonable_quantities_pass_including_office_orders(qty):
    assert reasons([make(1, qty=qty, note="طلب للمكتب")]) == [None]


@pytest.mark.parametrize("qty", [21, 32, 49, 52])
def test_absurd_quantities_are_abnormal(qty):
    assert reasons([make(1, qty=qty)]) == ["abnormal_qty"]


@pytest.mark.parametrize("qty", [0, -1, "two", "", None, 1.5, True])
def test_unusable_quantities_are_malformed(qty):
    assert reasons([make(1, qty=qty)]) == ["malformed"]


def test_unknown_or_missing_item_is_malformed():
    missing = make(2)
    del missing["item"]
    assert reasons([make(1, item="بيتزا"), missing, make(3, item="شاورما")]) == ["malformed"] * 3


def test_unknown_size_is_malformed():
    assert reasons([make(1, size="XXL"), make(2, size="")]) == ["malformed", "malformed"]


def test_bad_timestamp_or_missing_customer_is_malformed():
    assert reasons([make(1, ts="yesterday"), make(2, customer="  ")]) == ["malformed", "malformed"]


def test_decisions_do_not_depend_on_later_events():
    events = json.loads("[" + ",".join(json.dumps(make(i, ts=at(i))) for i in range(1, 12)) + "]")
    events[4]["qty"] = 99
    events[7]["device_id"] = events[6]["device_id"]
    full = filter_orders(events)
    for cut in range(1, len(events) + 1):
        assert filter_orders(events[:cut]) == full[:cut]


def test_rejected_event_still_counts_as_history_for_duplicates():
    first = make(1, device_id="dv-a", customer="سلمى", note="free gift card click now")
    retry = make(2, device_id="dv-a", customer="سلمى", note="free gift card click now", ts=at(4))
    assert reasons([first, retry]) == ["spam_content", "duplicate"]


def load_real():
    return [json.loads(line) for line in DATA.read_text(encoding="utf-8").splitlines()]


def test_real_file_matches_saved_decisions_and_counts():
    events = load_real()
    result = filter_orders(events)
    saved = [json.loads(line) for line in (DATA.parent / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result == saved
    counts = Counter(item["reason"] for item in result)
    assert counts == {None: 143, "burst": 84, "duplicate": 28, "spam_content": 20, "malformed": 14, "abnormal_qty": 6}


def test_real_file_has_no_lookahead():
    events = load_real()
    full = filter_orders(events)
    for cut in range(0, len(events) + 1, 7):
        assert filter_orders(events[:cut]) == full[:cut]


def test_first_event_of_a_burst_device_cannot_be_caught():
    events = [make(1, device_id="dv-x", customer="أحمد", ts=at(0)), make(2, device_id="dv-x", customer="منى", ts=at(1))]
    assert filter_orders(events[:1])[0]["decision"] == "accept"
    assert reasons(events) == [None, "burst"]


def test_mixed_timezones_do_not_crash():
    first = make(1, device_id="dv-a", customer="سلمى", ts="2026-11-20T10:00:00")
    second = make(2, device_id="dv-a", customer="سلمى", ts="2026-11-20T12:00:03+02:00")
    third = make(3, ts="2026-11-20T10:05:00Z")
    assert reasons([first, second, third]) == [None, "duplicate", None]


def test_garbage_values_never_raise():
    events = [
        make(1, item=["لاتيه"]),
        make(2, size={"x": 1}),
        make(3, note=5),
        make(4, note=["x"]),
        make(5, request_id=None),
        make(6, qty=10**30),
        "not a dict",
        None,
        {},
    ]
    result = filter_orders(events)
    assert len(result) == len(events)
    assert [item["reason"] for item in result] == [
        "malformed",
        "malformed",
        None,
        None,
        None,
        "abnormal_qty",
        "malformed",
        "malformed",
        "malformed",
    ]


def test_window_edge_is_inclusive_at_sixty_seconds():
    first = make(1, device_id="dv-a", customer="سلمى", ts=at(0))
    inside = make(2, device_id="dv-a", customer="سلمى", ts=at(60))
    outside = make(3, device_id="dv-a", customer="سلمى", ts=at(121))
    assert reasons([first, inside, outside]) == [None, "duplicate", None]


def test_reason_precedence():
    spam_and_big = make(1, note="free gift card click now", qty=50)
    assert reasons([spam_and_big]) == ["spam_content"]
    bad_item_and_dup = [make(2, request_id="rq-x"), make(3, request_id="rq-x", item="بيتزا")]
    assert reasons(bad_item_and_dup) == [None, "malformed"]
    dup_and_burst = [
        make(4, device_id="dv-b", customer="أحمد", ts=at(0)),
        make(5, device_id="dv-b", customer="منى", ts=at(2), request_id="rq-00000004"),
    ]
    assert reasons(dup_and_burst) == [None, "duplicate"]


def test_impatient_repeat_chain_is_not_blocked_forever():
    base = dict(device_id="dv-a", customer="سلمى")
    events = [make(i + 1, ts=at(i * 50), **base) for i in range(4)]
    assert reasons(events) == [None, "duplicate", None, "duplicate"]
