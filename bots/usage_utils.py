import calendar as cal_module
from datetime import date, timedelta

from django.db.models import Count, Exists, IntegerField, OuterRef, Subquery, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, ExtractMonth, ExtractYear, TruncDate
from django.utils import timezone

from .models import Bot, BotEvent, BotEventTypes, BotStates, Participant, Utterance

# Bots don't have an ended_at column. We synthesize it from the BotEvent that
# transitioned the bot to ENDED or FATAL_ERROR. This is the timestamp we use
# for time-based partitioning so that scheduled bots are bucketed by when they
# actually finished (and in-progress bots, which have no such event, are
# excluded).
_BOT_ENDED_AT_SUBQUERY = Subquery(
    BotEvent.objects.filter(
        bot=OuterRef("pk"),
        new_state__in=[BotStates.ENDED, BotStates.FATAL_ERROR],
    )
    .order_by("created_at")
    .values("created_at")[:1]
)

# Per-bot duration pulled from the metadata of the terminal BotEvent (the one
# that transitioned the bot to ENDED or FATAL_ERROR). Mirrors the event used
# by _BOT_ENDED_AT_SUBQUERY so duration and ended_at always come from the
# same row.
_BOT_DURATION_SUBQUERY = Subquery(
    BotEvent.objects.filter(
        bot=OuterRef("pk"),
        new_state__in=[BotStates.ENDED, BotStates.FATAL_ERROR],
    )
    .annotate(_dur=Cast(KeyTextTransform("bot_duration_seconds", "metadata"), output_field=IntegerField()))
    .order_by("created_at")
    .values("_dur")[:1],
    output_field=IntegerField(),
)


def _build_month_buckets(now):
    bucket_keys = []
    cur = now.replace(day=1)
    for _ in range(12):
        bucket_keys.append((cur.year, cur.month))
        if cur.month == 1:
            cur = cur.replace(year=cur.year - 1, month=12)
        else:
            cur = cur.replace(month=cur.month - 1)
    bucket_keys.reverse()

    start_year, start_month = bucket_keys[0]
    start_date = now.replace(
        year=start_year,
        month=start_month,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    labels = [date(y, m, 1).strftime("%b %Y") for y, m in bucket_keys]
    subtitle = "Bot activity over the last 12 months."

    date_ranges = []
    for y, m in bucket_keys:
        last_day = cal_module.monthrange(y, m)[1]
        date_ranges.append((date(y, m, 1).isoformat(), date(y, m, last_day).isoformat()))

    def counts_by_bucket(qs):
        result = {}
        for row in (
            qs.annotate(
                y=ExtractYear("ended_at"),
                m=ExtractMonth("ended_at"),
            )
            .values("y", "m")
            .annotate(count=Count("id", distinct=True))
        ):
            result[(row["y"], row["m"])] = row["count"]
        return result

    return bucket_keys, start_date, labels, subtitle, date_ranges, counts_by_bucket


def _build_week_buckets(now):
    today = now.date()
    current_monday = today - timedelta(days=today.weekday())
    bucket_keys = [current_monday - timedelta(weeks=i) for i in range(11, -1, -1)]
    start_date = now.replace(
        year=bucket_keys[0].year,
        month=bucket_keys[0].month,
        day=bucket_keys[0].day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    labels = [d.strftime("%b %d") for d in bucket_keys]
    subtitle = "Bot activity over the last 12 weeks."

    date_ranges = []
    for monday in bucket_keys:
        sunday = monday + timedelta(days=6)
        date_ranges.append((monday.isoformat(), sunday.isoformat()))

    def counts_by_bucket(qs):
        result = {}
        for row in qs.annotate(d=TruncDate("ended_at")).values("d").annotate(count=Count("id", distinct=True)):
            monday = row["d"] - timedelta(days=row["d"].weekday())
            result[monday] = result.get(monday, 0) + row["count"]
        return result

    return bucket_keys, start_date, labels, subtitle, date_ranges, counts_by_bucket


def _build_day_buckets(now):
    today = now.date()
    bucket_keys = [today - timedelta(days=i) for i in range(13, -1, -1)]
    start_date = now.replace(
        year=bucket_keys[0].year,
        month=bucket_keys[0].month,
        day=bucket_keys[0].day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    labels = [d.strftime("%b %d") for d in bucket_keys]
    subtitle = "Bot activity over the last 14 days."

    date_ranges = [(d.isoformat(), d.isoformat()) for d in bucket_keys]

    def counts_by_bucket(qs):
        result = {}
        for row in qs.annotate(d=TruncDate("ended_at")).values("d").annotate(count=Count("id", distinct=True)):
            result[row["d"]] = row["count"]
        return result

    return bucket_keys, start_date, labels, subtitle, date_ranges, counts_by_bucket


def _format_duration(seconds):
    if seconds == 0:
        return "0m"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h"
    return f"{minutes}m"


def _format_percent(value):
    if value == 0:
        return "0%"
    if value == 100:
        return "100%"
    return f"{value:.1f}%"


def _build_duration_aggregator(interval):
    if interval == "months":

        def durations_by_bucket(qs):
            result = {}
            for row in qs.annotate(y=ExtractYear("ended_at"), m=ExtractMonth("ended_at")).values("y", "m").annotate(total=Sum("bot_duration")):
                result[(row["y"], row["m"])] = row["total"] or 0
            return result

        return durations_by_bucket

    if interval == "weeks":

        def durations_by_bucket(qs):
            result = {}
            for row in qs.annotate(d=TruncDate("ended_at")).values("d").annotate(total=Sum("bot_duration")):
                monday = row["d"] - timedelta(days=row["d"].weekday())
                result[monday] = result.get(monday, 0) + (row["total"] or 0)
            return result

        return durations_by_bucket

    def durations_by_bucket(qs):
        result = {}
        for row in qs.annotate(d=TruncDate("ended_at")).values("d").annotate(total=Sum("bot_duration")):
            result[row["d"]] = row["total"] or 0
        return result

    return durations_by_bucket


def _build_heatmap_row(label, values, color, date_ranges, category_params, formatter=None, search_term=""):
    max_val = max(values) if values else 0
    cells = []
    for val, (start_str, end_str) in zip(values, date_ranges):
        intensity = val / max_val if max_val > 0 else 0
        bg = f"rgba({color}, {0.1 + intensity * 0.6})" if val > 0 else ""
        qs = f"?ended_at_start={start_str}&ended_at_end={end_str}"
        if category_params:
            qs += f"&{category_params}"
        if search_term:
            qs += f"&search={search_term}"
        display = formatter(val) if formatter else val
        cells.append({"value": val, "display": display, "bg": bg, "link": qs})
    return {"label": label, "cells": cells}


CATEGORY_FILTERS = {
    "Successful": "joined_meeting=yes&unexpected_error=no",
    "Successful With Other Participants": "joined_meeting=yes&unexpected_error=no&min_participants=2&min_duration=15",
    "Could Not Join": "joined_meeting=no&unexpected_error=no",
    "Unexpected Error": "unexpected_error=yes",
    "Transcript Generated": "joined_meeting=yes&unexpected_error=no&min_participants=2&min_duration=15&transcript=yes",
    "No Transcript": "joined_meeting=yes&unexpected_error=no&min_participants=2&min_duration=15&transcript=no",
    "Total": "",
}

PLATFORM_FILTERS = {
    "zoom": "zoom.us",
    "meet": "meet.google.com",
    "teams": "teams.",
}

TOTAL_COLOR = "13, 110, 253"

CATEGORY_SETS = ("default", "transcript")


def _build_categories(category_set, base_qs):
    """
    Build the list of categories for the requested category set.

    Returns a tuple of (categories, total_filter) where categories is a list of
    (label, queryset, color, filter_params) and total_filter is the query string
    the "Total" row should link to.
    """
    # Successful: joined the meeting and did not hit a fatal/unexpected error.
    successful_qs = base_qs.filter(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING).exclude(bot_events__event_type=BotEventTypes.FATAL_ERROR)

    if category_set == "transcript":
        # Only consider meetings that had at least two non-bot participants.
        # With fewer real participants there is little to transcribe, so a
        # missing transcript is expected rather than a sign of a problem.
        enough_participants = Exists(Participant.objects.filter(bot=OuterRef("pk"), is_the_bot=False).order_by().values("bot").annotate(count=Count("id")).filter(count__gte=2))
        # Also require the bot to have been in the meeting long enough that a
        # missing transcript would be meaningful (at least 15 minutes).
        transcribable_qs = successful_qs.filter(enough_participants).filter(bot_duration__gte=15 * 60)

        # Subcategories of successful: did the bot generate a transcript?
        # A transcript is "generated" when the bot has at least one utterance
        # that did not error (failure_data is null).
        has_transcript = Exists(Utterance.objects.filter(recording__bot=OuterRef("pk"), failure_data__isnull=True))
        transcript_generated_qs = transcribable_qs.filter(has_transcript)
        no_transcript_qs = transcribable_qs.filter(~has_transcript)
        categories = [
            ("Transcript Generated", transcript_generated_qs, "40, 167, 69", CATEGORY_FILTERS["Transcript Generated"]),
            ("No Transcript", no_transcript_qs, "255, 193, 7", CATEGORY_FILTERS["No Transcript"]),
        ]
        # The two transcript subcategories sum to all successful bots whose
        # meeting had at least two non-bot participants.
        return categories, CATEGORY_FILTERS["Successful With Other Participants"]

    fatal_error_qs = base_qs.filter(bot_events__event_type=BotEventTypes.FATAL_ERROR)
    could_not_join_qs = base_qs.exclude(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING).exclude(bot_events__event_type=BotEventTypes.FATAL_ERROR)
    categories = [
        ("Successful", successful_qs, "40, 167, 69", CATEGORY_FILTERS["Successful"]),
        ("Could Not Join", could_not_join_qs, "255, 193, 7", CATEGORY_FILTERS["Could Not Join"]),
        ("Unexpected Error", fatal_error_qs, "220, 53, 69", CATEGORY_FILTERS["Unexpected Error"]),
    ]
    return categories, CATEGORY_FILTERS["Total"]


def get_usage_data(project, interval, measure="count", platform="", category_set="default"):
    """
    Return the template context needed to render the usage heat map.

    Returns a dict with keys: column_labels, usage_rows, interval, measure, subtitle, platform, category_set.
    """
    if interval not in ("months", "weeks", "days"):
        interval = "months"
    if measure not in ("count", "time", "percent"):
        measure = "count"
    if platform not in PLATFORM_FILTERS:
        platform = ""
    if category_set not in CATEGORY_SETS:
        category_set = "default"

    platform_url_substring = PLATFORM_FILTERS.get(platform, "")

    now = timezone.now()
    builders = {
        "months": _build_month_buckets,
        "weeks": _build_week_buckets,
        "days": _build_day_buckets,
    }
    bucket_keys, start_date, labels, subtitle, date_ranges, counts_by_bucket = builders[interval](now)

    base_qs = Bot.objects.annotate(ended_at=_BOT_ENDED_AT_SUBQUERY).filter(project=project, ended_at__gte=start_date)

    # bot_duration is only needed when the "time" measure aggregates on it or
    # the "transcript" category set filters on it (minimum meeting duration), so
    # only pay for the subquery in those cases.
    if measure == "time" or category_set == "transcript":
        base_qs = base_qs.annotate(bot_duration=_BOT_DURATION_SUBQUERY)

    if measure == "time":
        aggregator = _build_duration_aggregator(interval)
        formatter = _format_duration
    else:
        aggregator = counts_by_bucket
        formatter = None

    if platform_url_substring:
        base_qs = base_qs.filter(meeting_url__icontains=platform_url_substring)

    categories, total_filter = _build_categories(category_set, base_qs)

    total_values = [0] * len(bucket_keys)
    category_values = []
    for label_text, qs, color, filter_params in categories:
        data = aggregator(qs)
        values = [data.get(key, 0) for key in bucket_keys]
        for i, v in enumerate(values):
            total_values[i] += v
        category_values.append((label_text, values, color, filter_params))

    rows = []
    if measure == "percent":
        for label_text, values, color, filter_params in category_values:
            pct_values = [round(v / total_values[i] * 100, 1) if total_values[i] > 0 else 0 for i, v in enumerate(values)]
            rows.append(_build_heatmap_row(label_text, pct_values, color, date_ranges, filter_params, formatter=_format_percent, search_term=platform_url_substring))
        rows.append(_build_heatmap_row("Total", [100.0 if t > 0 else 0 for t in total_values], TOTAL_COLOR, date_ranges, total_filter, formatter=_format_percent, search_term=platform_url_substring))
    else:
        for label_text, values, color, filter_params in category_values:
            rows.append(_build_heatmap_row(label_text, values, color, date_ranges, filter_params, formatter=formatter, search_term=platform_url_substring))
        rows.append(_build_heatmap_row("Total", total_values, TOTAL_COLOR, date_ranges, total_filter, formatter=formatter, search_term=platform_url_substring))

    clipboard_dates = [dr[0] for dr in date_ranges]

    return {
        "column_labels": labels,
        "usage_rows": rows,
        "interval": interval,
        "measure": measure,
        "subtitle": subtitle,
        "clipboard_dates": clipboard_dates,
        "platform": platform,
        "category_set": category_set,
    }
