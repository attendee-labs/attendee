import math
from dataclasses import dataclass
from typing import List, Tuple, TypedDict


@dataclass
class Word:
    text: str
    start: int  # timestamp in milliseconds
    end: int  # timestamp in milliseconds


@dataclass
class AnnotatedWord(Word):
    utterance_label: int
    is_utterance_start: bool
    is_start_of_new_sentence: bool


@dataclass
class SpeechStartEvent:
    participant_id: int
    timestamp: int  # timestamp in milliseconds


@dataclass
class MatchedSpeechStartEvent(SpeechStartEvent):
    matched_word_index: int


@dataclass
class DiarizedWord(Word):
    participant_id: int


class DiarizationResult(TypedDict):
    """Dictionary representing a diarization segment result."""

    text: str
    speaker: str


WORD_MERGE_DIST_MS = 100
WORD_MATCH_DIST_MS = 1000


def create_annotated_words(words: List[Word]) -> List[AnnotatedWord]:
    """
    Add utterance_label to each word based on timing gaps.
    Words are in the same utterance if they are less than WORD_MERGE_DIST_MS apart.

    Args:
        words: List of word dictionaries with 'start' and 'end' timestamps in ms

    Returns:
        List of word dictionaries with added 'utterance_label' field
    """
    if not words:
        return words

    annotated_words = list(map(lambda x: AnnotatedWord(x.text, x.start, x.end, -1, False, False), words))

    # Initialize the first word
    utterance_label = 0
    annotated_words[0].utterance_label = utterance_label
    annotated_words[0].is_utterance_start = True
    annotated_words[0].is_start_of_new_sentence = True

    # Process remaining words
    for i in range(1, len(annotated_words)):
        current_word = annotated_words[i]
        previous_word = annotated_words[i - 1]

        # Calculate gap between end of previous word and start of current word
        # If gap is >= WORD_MERGE_DIST_MS, start a new utterance
        gap_ms = current_word.start - previous_word.end

        if gap_ms >= WORD_MERGE_DIST_MS:
            utterance_label += 1
            current_word.is_utterance_start = True
        else:
            current_word.is_utterance_start = False

        current_word.utterance_label = utterance_label

        current_word.is_start_of_new_sentence = previous_word.text and previous_word.text.strip()[-1] in [".", "!", "?"]

    return annotated_words


# Offsets the speaker change timestamps by the given amount
def compute_shifted_speaker_change_events(speaker_events: List[SpeechStartEvent], offset_ms: int) -> List[SpeechStartEvent]:
    return list(map(lambda x: SpeechStartEvent(x.participant_id, x.timestamp - offset_ms), speaker_events))


def compute_diarization_and_score_for_offset(words: List[AnnotatedWord], speaker_events: List[SpeechStartEvent], offset_ms: int) -> Tuple[float, List[DiarizedWord]]:
    shifted_speaker_events = compute_shifted_speaker_change_events(speaker_events, offset_ms)
    score = 0
    first_eligible_word_to_match_index = 0
    matched_speaker_events = []

    # Take greedy approach towards building a matching between speaker events and words
    # A speaker event can match to exactly one word or no word
    # A word can match to exactly one speaker event or no speaker event
    # If speaker event a came before speaker event b, then the matched word for a must come before the matched word for b
    for speaker_event in shifted_speaker_events:
        best_matched_word_index = None
        best_matched_word_score = 0

        # Iterate over all possible words this speaker event could match to
        for possible_matched_word_index in range(first_eligible_word_to_match_index, len(words)):
            possible_matched_word = words[possible_matched_word_index]
            # The word's start must be within WORD_MATCH_DIST_MS of the speaker event's timestamp to be eligible to match
            distance_between_speaker_event_and_possible_matched_word = math.fabs(possible_matched_word.start - speaker_event.timestamp)
            if distance_between_speaker_event_and_possible_matched_word > WORD_MATCH_DIST_MS:
                continue

            # We expect a speaker change to coincide with an utterance start
            if possible_matched_word.is_utterance_start:
                possible_matched_word_score = 1
            # A new sentence start is almost as expected
            elif possible_matched_word.is_start_of_new_sentence:
                possible_matched_word_score = 0.5
            # It's less expected to coincide with a word in the middle of a sentence / utterance
            # penalize based on how far away it is. If it coincides perfectly, it gets 0.25
            else:
                normalized_distance = distance_between_speaker_event_and_possible_matched_word / WORD_MATCH_DIST_MS
                possible_matched_word_score = 0.25 * (1 - normalized_distance)

            if possible_matched_word_score > best_matched_word_score or best_matched_word_index is None:
                best_matched_word_index = possible_matched_word_index
                best_matched_word_score = possible_matched_word_score

        # It's possible that no word was found to match this speaker event, because it was too far away from any words
        # But if we found a match, then assign it
        if best_matched_word_index is not None:
            score += best_matched_word_score
            first_eligible_word_to_match_index = best_matched_word_index + 1

        matched_speaker_events.append(MatchedSpeechStartEvent(speaker_event.participant_id, speaker_event.timestamp, best_matched_word_index))

    diarized_words = []
    for matched_speaker_event_index, matched_speaker_event in enumerate(matched_speaker_events):
        if matched_speaker_event.matched_word_index is None:
            continue

        # Loop over the remaining matched speaker events and find the first one whose matched_word_index is not None
        next_matched_word_index = next((m.matched_word_index for m in matched_speaker_events[(matched_speaker_event_index + 1) :] if m.matched_word_index is not None), len(words))

        # Assign all the words up until the next matched word to the matched speaker event
        for word_index in range(matched_speaker_event.matched_word_index, next_matched_word_index):
            diarized_words.append(DiarizedWord(words[word_index].text, words[word_index].start, words[word_index].end, matched_speaker_event.participant_id))

    return score, diarized_words


def pretty_print_diarization(diarization: List[DiarizedWord]):
    if not diarization:
        return

    current_utterance = {"participant_id": diarization[0].participant_id, "text": ""}
    for word in diarization:
        if current_utterance["participant_id"] != word.participant_id:
            print(f"Participant {current_utterance['participant_id']} said: {current_utterance['text']}")
            current_utterance = {"participant_id": word.participant_id, "text": ""}
        current_utterance["text"] += word.text + " "

    print(f"Participant {current_utterance['participant_id']} said: {current_utterance['text']}")


def diarize_words(words: List[Word], speech_start_events: List[SpeechStartEvent], base_offset_ms: int) -> List[DiarizedWord]:
    # Enrich words with utterance labels and start of new sentence flags
    annotated_words = create_annotated_words(words)

    # Sample offsets from -500 to 500 milliseconds in steps of 10 milliseconds
    offsets_ms = list(range(-500, 501, 10))
    # offsets_ms = list(range(-2000, 2000, 100))
    best_score = 0
    best_diarization = None
    best_offset_ms = 0

    for offset_ms in offsets_ms:
        score, diarization = compute_diarization_and_score_for_offset(annotated_words, speech_start_events, offset_ms + base_offset_ms)
        print(f"Offset: {offset_ms}ms, Score: {score}")
        if score > best_score or best_diarization is None:
            best_score = score
            best_diarization = diarization
            best_offset_ms = offset_ms

    print(f"Best diarization had score {best_score} with offset {best_offset_ms}ms")
    pretty_print_diarization(best_diarization)

    return best_diarization
