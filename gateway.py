import re
from collections import defaultdict
from datetime import datetime, timezone

MENU = {"قهوة اليوم", "لاتيه", "فلات وايت", "كولد برو", "إسبريسو", "V60"}
SIZES = {"S", "M", "L"}

DUPLICATE_WINDOW = 60
BURST_WINDOW = 60
MAX_QTY = 20

MAPS_HOSTS = {"maps.app.goo.gl", "maps.google.com"}
GOOGLE_HOSTS = {"google.com", "www.google.com"}

TLDS = (
    "com|net|org|xyz|top|click|info|io|co|ly|app|me|shop|site|online|store|biz|link|live|ru|cn|tk|cc|ws|example"
)
LINK_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|\b(?:wa\.me|t\.me|telegram\.me|bit\.ly|tinyurl\.com)/?\S*"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + TLDS + r")\b/?\S*)",
    re.IGNORECASE,
)
URL_PARTS = re.compile(r"^(?:https?://)?([^/\s:?#]+)(/[^\s]*)?", re.IGNORECASE)

PROMO_ARABIC_WORDS = {"ربح", "اربح", "قروض", "قرض", "متابعين", "تسويق"}
PROMO_ARABIC_PHRASES = ("اضغط هنا", "هدية مجانية", "ربح مضمون")
PROMO_LATIN = re.compile(
    r"\b(win|winner|gift|click|giveaway|followers|crypto|bitcoin|forex|loan|loans|vip|casino|bonus"
    r"|iphone|lorem|ipsum|promo)\b"
    r"|\bfree\s+(?:gift|money|iphone|followers|bitcoin|crypto)\b"
    r"|\b(?:buy|order)\s+now\b|\bget\s+rich\b|\bearn\s*\$?\d|\d+\s*%\s*off\b",
    re.IGNORECASE,
)
ARABIC_TOKEN = re.compile(r"[ء-ي]+")

KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
KEYBOARD_RUN = 5
KEYBOARD_WORD = re.compile(r"[a-z]{4,}")
REPEATED_RUN = re.compile(r"([^\d\sه.!?،\-])\1{7,}")
LATIN_WORD = re.compile(r"[a-z]{5,}")
VOWELS = set("aeiouy")


def parse_time(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_valid_qty(qty):
    return isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1


def is_malformed(event, moment):
    if not isinstance(event.get("id"), str) or moment is None:
        return True
    if not isinstance(event.get("device_id"), str) or not event["device_id"]:
        return True
    customer = event.get("customer")
    if not isinstance(customer, str) or not customer.strip():
        return True
    item = event.get("item")
    if not isinstance(item, str) or item not in MENU:
        return True
    size = event.get("size")
    if not isinstance(size, str) or size not in SIZES:
        return True
    return not is_valid_qty(event.get("qty"))


def link_is_unsafe(text):
    for match in LINK_PATTERN.finditer(text):
        parts = URL_PARTS.match(match.group(0))
        if parts is None:
            return True
        host = parts.group(1).lower()
        path = parts.group(2) or ""
        if host in MAPS_HOSTS:
            continue
        if host in GOOGLE_HOSTS and path.startswith("/maps"):
            continue
        return True
    return False


def has_promo_words(text):
    for token in ARABIC_TOKEN.findall(text):
        if token in PROMO_ARABIC_WORDS or (token.startswith("و") and token[1:] in PROMO_ARABIC_WORDS):
            return True
    return any(phrase in text for phrase in PROMO_ARABIC_PHRASES) or bool(PROMO_LATIN.search(text))


def looks_like_keyboard_mash(text):
    for row in KEYBOARD_ROWS:
        for start in range(len(row) - KEYBOARD_RUN + 1):
            if row[start:start + KEYBOARD_RUN] in text:
                return True
    for word in KEYBOARD_WORD.findall(text):
        if any(word in row for row in KEYBOARD_ROWS):
            return True
    return False


def has_vowelless_word(text):
    return any(not (set(word) & VOWELS) for word in LATIN_WORD.findall(text))


def is_spam(note):
    if not isinstance(note, str) or not note.strip():
        return False
    text = note.lower()
    if link_is_unsafe(text):
        return True
    text = LINK_PATTERN.sub(" ", text)
    if has_promo_words(text) or REPEATED_RUN.search(text):
        return True
    return looks_like_keyboard_mash(text) or has_vowelless_word(text)


def clean_note(event):
    note = event.get("note")
    return note.strip() if isinstance(note, str) else ""


def fingerprint(event):
    return (
        event["device_id"],
        event["customer"].strip(),
        event["item"],
        event["size"],
        event["qty"],
        clean_note(event),
    )


class Gateway:
    def __init__(self):
        self.request_ids = set()
        self.last_seen = {}
        self.recent_by_device = defaultdict(list)

    def is_duplicate(self, event, moment):
        request_id = event.get("request_id")
        if isinstance(request_id, str) and request_id in self.request_ids:
            return True
        last = self.last_seen.get(fingerprint(event))
        return last is not None and abs((moment - last).total_seconds()) <= DUPLICATE_WINDOW

    def is_burst(self, event, moment):
        customer = event["customer"].strip()
        for seen_at, seen_customer in self.recent_by_device[event["device_id"]]:
            if abs((moment - seen_at).total_seconds()) <= BURST_WINDOW and seen_customer != customer:
                return True
        return False

    def remember(self, event, moment, duplicate):
        request_id = event.get("request_id")
        if isinstance(request_id, str):
            self.request_ids.add(request_id)
        if not duplicate:
            self.last_seen[fingerprint(event)] = moment
        history = self.recent_by_device[event["device_id"]]
        history.append((moment, event["customer"].strip()))
        history[:] = [item for item in history if abs((moment - item[0]).total_seconds()) <= BURST_WINDOW]

    def decide(self, event):
        if not isinstance(event, dict):
            return None, "malformed"
        moment = parse_time(event.get("ts"))
        if is_malformed(event, moment):
            return event.get("id"), "malformed"
        duplicate = self.is_duplicate(event, moment)
        reason = None
        if duplicate:
            reason = "duplicate"
        elif self.is_burst(event, moment):
            reason = "burst"
        elif is_spam(event.get("note")):
            reason = "spam_content"
        elif event["qty"] > MAX_QTY:
            reason = "abnormal_qty"
        self.remember(event, moment, duplicate)
        return event["id"], reason


def filter_orders(events):
    gateway = Gateway()
    decisions = []
    for event in events:
        event_id, reason = gateway.decide(event)
        decisions.append(
            {
                "id": event_id,
                "decision": "accept" if reason is None else "reject",
                "reason": reason,
            }
        )
    return decisions
