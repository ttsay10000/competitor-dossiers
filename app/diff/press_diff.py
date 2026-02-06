import hashlib


def press_identity(item: dict) -> str:
    if item.get("url"):
        return item["url"]
    key = f"{item.get('title','')}|{item.get('date','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def diff_items(previous: list[dict], current: list[dict]) -> dict[str, list[dict]]:
    prev_map = {press_identity(item): item for item in previous}
    curr_map = {press_identity(item): item for item in current}

    added = [item for key, item in curr_map.items() if key not in prev_map]
    removed = [item for key, item in prev_map.items() if key not in curr_map]

    return {"added": added, "removed": removed}
