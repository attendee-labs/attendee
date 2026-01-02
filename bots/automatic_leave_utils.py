import re
from typing import Iterable

from .automatic_leave_configuration import AutomaticLeaveConfiguration


def participant_is_another_bot(participant_full_name, participant_is_the_bot, automatic_leave_configuration: AutomaticLeaveConfiguration):
    # If the participant is the bot that is being run, then they do not count as another bot, they are OUR bot.
    if participant_is_the_bot:
        return False

    # We'll use the bot keywords heuristic to determine if the participant is another bot
    if not automatic_leave_configuration.bot_keywords:
        return False

    if string_contains_keywords(participant_full_name, automatic_leave_configuration.bot_keywords):
        return True

    # If no patterns match, then the participant is not another bot
    return False


# Split a string into a list of lower case words, splitting on spaces, hyphens, and underscores
def split_string_into_lower_case_words(string: str) -> list[str]:
    return [w.lower() for w in re.split(r"[\s\-_]+", string) if w]


def string_contains_keywords(string: str, keywords_list: Iterable[str]) -> bool:
    """
    Returns True if `string` contains ANY keyword from `keywords_list` as a contiguous
    sequence of space-delimited words (case-insensitive).

    - Delimiter is a single space (per prompt).
    - Multi-word keywords must appear in the same order and contiguously.
      e.g. "Bob Johnson senior" matches "Bob Johnson"
           "Bob senior Johnson" does NOT match "Bob Johnson"
    """
    words = split_string_into_lower_case_words(string)
    if not words:
        return False

    for kw in keywords_list:
        kw_words = split_string_into_lower_case_words(kw)
        if not kw_words:
            continue

        k = len(kw_words)
        if k > len(words):
            continue

        # Sliding window exact match for contiguous sequence
        for i in range(len(words) - k + 1):
            if words[i : i + k] == kw_words:
                return True

    return False
