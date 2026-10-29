from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, tag
from selenium.common.exceptions import TimeoutException

from bots.google_meet_bot_adapter.google_meet_bot_adapter import GoogleMeetBotAdapter
from bots.google_meet_bot_adapter.google_meet_closed_captions_language_ids import GOOGLE_MEET_CLOSED_CAPTIONS_LANGUAGE_IDS
from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods
from bots.serializers import TRANSCRIPTION_SETTINGS_SCHEMA


class GoogleMeetUIMethodsWithDriver(GoogleMeetUIMethods):
    def __init__(self, driver):
        self.driver = driver


LANGUAGE_SENT_TO_MEET_SCRIPT = "return window.closedCaptionsLanguageSentToMeet ?? null;"


def create_driver(*languages_sent_to_meet):
    """Driver whose payload saw Meet send these languages over the captions_v2 data channel, one per read. The last one stays."""
    languages_sent_to_meet = list(languages_sent_to_meet)

    def execute_script(script, *args):
        if script == LANGUAGE_SENT_TO_MEET_SCRIPT:
            return languages_sent_to_meet.pop(0) if len(languages_sent_to_meet) > 1 else languages_sent_to_meet[0]
        if script == "return clickLanguageOption(arguments[0]);":
            # clickLanguageOption() found and clicked the language option
            return True
        raise AssertionError(f"Unexpected script: {script}")

    driver = MagicMock()
    driver.execute_script.side_effect = execute_script
    return driver


@tag("google_meet_tests")
class TestGoogleMeetClosedCaptionsLanguageIsAlreadySelected(SimpleTestCase):
    def test_returns_true_when_language_was_sent_to_meet(self):
        self.assertTrue(GoogleMeetUIMethodsWithDriver(create_driver("es-ES")).closed_captions_language_is_already_selected("es-ES"))

    def test_waits_for_meet_to_send_the_language(self):
        driver = create_driver(None, None, "es-ES")
        self.assertTrue(GoogleMeetUIMethodsWithDriver(driver).closed_captions_language_is_already_selected("es-ES"))
        self.assertEqual(driver.execute_script.call_count, 3)

    def test_returns_false_when_a_different_language_was_sent_to_meet(self):
        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.WebDriverWait") as web_driver_wait:
            web_driver_wait.return_value.until.side_effect = TimeoutException()
            self.assertFalse(GoogleMeetUIMethodsWithDriver(create_driver("en-US")).closed_captions_language_is_already_selected("es-ES"))

    def test_returns_false_when_no_language_was_sent_to_meet(self):
        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.WebDriverWait") as web_driver_wait:
            web_driver_wait.return_value.until.side_effect = TimeoutException()
            self.assertFalse(GoogleMeetUIMethodsWithDriver(create_driver(None)).closed_captions_language_is_already_selected("es-ES"))


@tag("google_meet_tests")
class TestGoogleMeetSelectLanguage(SimpleTestCase):
    def test_does_not_open_settings_dialog_when_language_is_already_selected(self):
        driver = create_driver("es-ES")

        with patch.object(GoogleMeetUIMethods, "closed_captions_language_is_already_selected", return_value=True):
            GoogleMeetUIMethodsWithDriver(driver).select_language("es-ES")

        driver.find_element.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_selects_language_in_settings_dialog_when_language_is_not_selected(self):
        driver = create_driver("en-US")

        with patch.object(GoogleMeetUIMethods, "closed_captions_language_is_already_selected", return_value=False):
            GoogleMeetUIMethodsWithDriver(driver).select_language("es-ES")

        driver.execute_script.assert_called_once_with("return clickLanguageOption(arguments[0]);", "es-ES")


@tag("google_meet_tests")
class TestGoogleMeetClosedCaptionsLanguageIds(SimpleTestCase):
    def test_every_supported_language_has_an_id(self):
        supported_languages = TRANSCRIPTION_SETTINGS_SCHEMA["properties"]["meeting_closed_captions"]["properties"]["google_meet_language"]["enum"]
        self.assertEqual(set(GOOGLE_MEET_CLOSED_CAPTIONS_LANGUAGE_IDS), set(supported_languages))

    def test_ids_are_unique(self):
        ids = list(GOOGLE_MEET_CLOSED_CAPTIONS_LANGUAGE_IDS.values())
        self.assertEqual(len(ids), len(set(ids)))

    def initial_data_code(self, language):
        adapter = GoogleMeetBotAdapter.__new__(GoogleMeetBotAdapter)
        adapter.modify_dom_for_video_recording = False
        adapter.disable_incoming_video = False
        adapter.google_meet_closed_captions_language = language
        return adapter.subclass_specific_initial_data_code()

    def test_initial_data_contains_language_id(self):
        self.assertIn("closedCaptionsLanguageId: 3,", self.initial_data_code("es-ES"))

    def test_initial_data_contains_null_language_id_when_no_language_is_set(self):
        self.assertIn("closedCaptionsLanguageId: null,", self.initial_data_code(None))
