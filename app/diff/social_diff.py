"""Diff for social channel: identify new/removed posts by id."""


def social_post_identity(post: dict) -> str:
    if post.get("id"):
        return post["id"]
    if post.get("url"):
        return post["url"]
    return f"{post.get('text', '')[:100]}|{post.get('published_at', '')}"


def diff_social_posts(previous: list[dict], current: list[dict]) -> dict[str, list[dict]]:
    prev_map = {social_post_identity(p): p for p in previous}
    curr_map = {social_post_identity(p): p for p in current}
    added = [p for key, p in curr_map.items() if key not in prev_map]
    removed = [p for key, p in prev_map.items() if key not in curr_map]
    return {"added": added, "removed": removed}
