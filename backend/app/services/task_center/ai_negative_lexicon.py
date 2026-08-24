from __future__ import annotations


def enabled_negative_phrases(gate_config: dict, route: str) -> tuple[str, ...]:
    lexicon = dict(gate_config or {}).get("negative_lexicon")
    if lexicon is None:
        return ()
    if not isinstance(lexicon, dict) or not str(lexicon.get("version") or ""):
        raise ValueError("negative_lexicon_invalid")
    entries = lexicon.get("entries")
    if not isinstance(entries, list):
        raise ValueError("negative_lexicon_entries_invalid")
    phrases: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("negative_lexicon_entry_invalid")
        if not _entry_applies(entry, route):
            continue
        phrase = str(entry.get("phrase") or "").strip()
        if not phrase:
            raise ValueError("negative_lexicon_phrase_missing")
        phrases.append(phrase)
    return tuple(dict.fromkeys(phrases))


def _entry_applies(entry: dict, route: str) -> bool:
    if entry.get("enabled") is not True or entry.get("scope") != "output":
        return False
    if entry.get("match_type") != "contains":
        raise ValueError("negative_lexicon_match_type_invalid")
    routes = entry.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("negative_lexicon_routes_invalid")
    return "*" in routes or route in routes


__all__ = ["enabled_negative_phrases"]
