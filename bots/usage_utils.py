import calendar as cal_module
from datetime import date, timedelta

from django.db.models import Count, IntegerField, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, ExtractMonth, ExtractYear, TruncDate
from django.utils import timezone

from .models import Bot, BotEvent, BotEventTypes


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
                y=ExtractYear("created_at"),
                m=ExtractMonth("created_at"),
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
        for row in qs.annotate(d=TruncDate("created_at")).values("d").annotate(count=Count("id", distinct=True)):
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
        for row in qs.annotate(d=TruncDate("created_at")).values("d").annotate(count=Count("id", distinct=True)):
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


_DURATION_SUM = Sum(Cast(KeyTextTransform("bot_duration_seconds", "metadata"), output_field=IntegerField()))


def _build_duration_aggregator(interval):
    if interval == "months":

        def durations_by_bucket(event_qs):
            result = {}
            for row in event_qs.annotate(y=ExtractYear("created_at"), m=ExtractMonth("created_at")).values("y", "m").annotate(total=_DURATION_SUM):
                result[(row["y"], row["m"])] = row["total"] or 0
            return result

        return durations_by_bucket

    if interval == "weeks":

        def durations_by_bucket(event_qs):
            result = {}
            for row in event_qs.annotate(d=TruncDate("created_at")).values("d").annotate(total=_DURATION_SUM):
                monday = row["d"] - timedelta(days=row["d"].weekday())
                result[monday] = result.get(monday, 0) + (row["total"] or 0)
            return result

        return durations_by_bucket

    def durations_by_bucket(event_qs):
        result = {}
        for row in event_qs.annotate(d=TruncDate("created_at")).values("d").annotate(total=_DURATION_SUM):
            result[row["d"]] = row["total"] or 0
        return result

    return durations_by_bucket


def _build_heatmap_row(label, values, color, date_ranges, category_params, formatter=None, search_term=""):
    max_val = max(values) if values else 0
    cells = []
    for val, (start_str, end_str) in zip(values, date_ranges):
        intensity = val / max_val if max_val > 0 else 0
        bg = f"rgba({color}, {0.1 + intensity * 0.6})" if val > 0 else ""
        qs = f"?start_date={start_str}&end_date={end_str}"
        if category_params:
            qs += f"&{category_params}"
        if search_term:
            qs += f"&search={search_term}"
        display = formatter(val) if formatter else val
        cells.append({"value": val, "display": display, "bg": bg, "link": qs})
    return {"label": label, "cells": cells}


CATEGORY_FILTERS = {
    "Successful": "joined_meeting=yes&unexpected_error=no",
    "Could Not Join": "joined_meeting=no&unexpected_error=no",
    "Unexpected Error": "unexpected_error=yes",
    "Total": "",
}

PLATFORM_FILTERS = {
    "zoom": "zoom.us",
    "meet": "meet.google.com",
    "teams": "teams.",
}


def get_usage_data(project, interval, measure="count", platform=""):
    """
    Return the template context needed to render the usage heat map.

    Returns a dict with keys: column_labels, usage_rows, interval, measure, subtitle, platform.
    """
    if interval not in ("months", "weeks", "days"):
        interval = "months"
    if measure not in ("count", "time", "percent"):
        measure = "count"
    if platform not in PLATFORM_FILTERS:
        platform = ""

    platform_url_substring = PLATFORM_FILTERS.get(platform, "")

    now = timezone.now()
    builders = {
        "months": _build_month_buckets,
        "weeks": _build_week_buckets,
        "days": _build_day_buckets,
    }
    bucket_keys, start_date, labels, subtitle, date_ranges, counts_by_bucket = builders[interval](now)

    if measure == "time":
        durations_by_bucket = _build_duration_aggregator(interval)
        event_qs = BotEvent.objects.filter(
            bot__project=project,
            bot__created_at__gte=start_date,
            event_type=BotEventTypes.POST_PROCESSING_COMPLETED,
        )
        if platform_url_substring:
            event_qs = event_qs.filter(bot__meeting_url__icontains=platform_url_substring)
        total_durations = durations_by_bucket(event_qs)
        total_values = [total_durations.get(key, 0) for key in bucket_keys]
        rows = [_build_heatmap_row("Total", total_values, "13, 110, 253", date_ranges, CATEGORY_FILTERS["Total"], formatter=_format_duration, search_term=platform_url_substring)]
    else:
        base_qs = Bot.objects.filter(project=project, created_at__gte=start_date)
        if platform_url_substring:
            base_qs = base_qs.filter(meeting_url__icontains=platform_url_substring)
        fatal_error = counts_by_bucket(base_qs.filter(bot_events__event_type=BotEventTypes.FATAL_ERROR))
        successful = counts_by_bucket(base_qs.filter(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING).exclude(bot_events__event_type=BotEventTypes.FATAL_ERROR))
        could_not_join = counts_by_bucket(base_qs.exclude(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING).exclude(bot_events__event_type=BotEventTypes.FATAL_ERROR))

        categories = [
            ("Successful", successful, "40, 167, 69"),
            ("Could Not Join", could_not_join, "255, 193, 7"),
            ("Unexpected Error", fatal_error, "220, 53, 69"),
        ]

        rows = []
        total_values = [0] * len(bucket_keys)
        category_values = []
        for label_text, data, color in categories:
            values = [data.get(key, 0) for key in bucket_keys]
            for i, v in enumerate(values):
                total_values[i] += v
            category_values.append((label_text, values, color))

        if measure == "percent":
            for label_text, values, color in category_values:
                pct_values = [round(v / total_values[i] * 100, 1) if total_values[i] > 0 else 0 for i, v in enumerate(values)]
                rows.append(_build_heatmap_row(label_text, pct_values, color, date_ranges, CATEGORY_FILTERS[label_text], formatter=_format_percent, search_term=platform_url_substring))
            rows.append(_build_heatmap_row("Total", [100.0 if t > 0 else 0 for t in total_values], "13, 110, 253", date_ranges, CATEGORY_FILTERS["Total"], formatter=_format_percent, search_term=platform_url_substring))
        else:
            for label_text, values, color in category_values:
                rows.append(_build_heatmap_row(label_text, values, color, date_ranges, CATEGORY_FILTERS[label_text], search_term=platform_url_substring))
            rows.append(_build_heatmap_row("Total", total_values, "13, 110, 253", date_ranges, CATEGORY_FILTERS["Total"], search_term=platform_url_substring))

    clipboard_dates = [dr[0] for dr in date_ranges]

    return {
        "column_labels": labels,
        "usage_rows": rows,
        "interval": interval,
        "measure": measure,
        "subtitle": subtitle,
        "clipboard_dates": clipboard_dates,
        "platform": platform,
    }
