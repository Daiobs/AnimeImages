"""Pure caption presentation shared by work and download naming."""
import re


_TOPIC = re.compile(r"(?<!\S)[#＃]([^\s#＃]+)")
_REPEATED_LEAD = re.compile(r"^(?P<lead>\S{4,60})\s+(?P=lead)\s+(?P<tail>.+)$", re.S)


def _chinese_lead(value):
    return bool(re.search(r"[\u3400-\u9fff]", value) and not re.search(r"[A-Za-z]", value))


def _joined_caption(value):
    """Recognize title/body joins only when the body repeats the complete title."""
    title, separator, body = value.partition("|||")
    title, body = title.strip(), body.strip()
    if not separator or not title or len(title) > 80 or "\n" in title:
        return None
    if body == title:
        return title, ""
    if body.startswith(title) and body[len(title):len(title) + 1].isspace():
        return title, body[len(title):].strip()
    return None


def caption_presentation(title, description=""):
    """Present text known to be a social caption, retaining its complete source."""
    raw_title = str(title or "").strip()
    raw_description = str(description or "").strip()
    if raw_description == raw_title:
        raw_description = ""
    caption = raw_title or raw_description
    extra = raw_description if raw_title else ""
    topics = list(dict.fromkeys(
        match.group(1).rstrip("，。！？,.;；:：!")
        for text in (caption, extra)
        for match in _TOPIC.finditer(text)
        if match.group(1).rstrip("，。！？,.;；:：!")
    ))
    body = _TOPIC.sub("", caption).strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    display_title = " ".join(body.split())
    remainder = ""
    derived = False
    joined = _joined_caption(caption)
    repeated = _REPEATED_LEAD.fullmatch(caption)
    if joined:
        display_title, remainder = joined[0], _TOPIC.sub("", joined[1]).strip()
        derived = True
    elif (repeated and _chinese_lead(repeated['lead'])
            and all(re.fullmatch(r"[@＠#＃][^\s]+", token) for token in repeated['tail'].split())):
        # Old cached labels can contain the title twice followed by social tags.
        display_title, remainder, derived = repeated['lead'], body, True
    elif len(lines) > 1 and len(lines[0]) <= 80:
        display_title, remainder = lines[0], "\n".join(lines[1:])
        derived = True
    elif _TOPIC.search(caption) and len(display_title) > 60:
        lead, separator, tail = display_title.partition(" ")
        if separator and 4 <= len(lead) <= 60 and len(tail) >= 20 and _chinese_lead(lead):
            display_title, remainder, derived = lead, tail.strip(), True
    details = [remainder]
    if extra:
        extra_body = _TOPIC.sub("", extra).strip()
        # Compare presentation whitespace without rewriting either source field.
        if extra_body.split() not in (body.split(), remainder.split()):
            details.append(extra_body)
    if len(display_title) > 200 and not remainder:
        details.insert(0, body)
    return {
        "title": display_title[:200],
        "description": "\n".join(part for part in details if part),
        "topics": topics,
        "caption": "\n".join(part for part in (caption, extra) if part),
        "title_derived": derived,
    }


def work_caption_presentation(title="", description=""):
    """Prefer an explicit title; derive only from body or recognizable captions.

    Unlike a known social-caption field, an ordinary title may legitimately
    contain hash signs and spaces. Keep it unless the caption has a clear lead.
    Raw body and caption remain available independently of the bounded title.
    """
    raw_title = str(title or "").strip()
    raw_description = str(description or "").strip()
    result = caption_presentation(raw_title, raw_description)
    distinct_description = raw_description and raw_description != raw_title
    joined = _joined_caption(raw_title or raw_description)
    if raw_title and ((distinct_description and not joined) or not result['title_derived']):
        result['title'] = " ".join(raw_title.split())[:200]
        result['title_derived'] = False
        result['topics'] = (caption_presentation("", raw_description)['topics']
                            if distinct_description else [])
    if raw_description and not joined:
        result['description'] = raw_description
    return result


def work_caption_metadata(title="", description=""):
    """Bound stored presentation text to the existing 2000-character body budget."""
    result = work_caption_presentation(title, description)
    return {**result, 'description': result['description'][:2000],
            'caption': result['caption'][:2000],
            'topics': [topic[:120] for topic in result['topics'][:16]]}
