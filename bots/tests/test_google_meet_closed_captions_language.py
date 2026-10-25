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


def create_driver(selected_language):
    """Driver whose captions bar "Meeting language" list has selected_language selected."""
    selected_language_option = MagicMock()
    selected_language_option.get_attribute.return_value = selected_language
    driver = MagicMock()
    driver.find_element.return_value = selected_language_option
    # clickLanguageOption() found and clicked the language option
    driver.execute_script.return_value = True
    return driver


@tag("google_meet_tests")
class TestGoogleMeetClosedCaptionsLanguageIsAlreadySelected(SimpleTestCase):
    def test_returns_true_when_language_is_selected(self):
        self.assertTrue(GoogleMeetUIMethodsWithDriver(create_driver("es-ES")).closed_captions_language_is_already_selected("es-ES"))

    def test_returns_false_when_a_different_language_is_selected(self):
        self.assertFalse(GoogleMeetUIMethodsWithDriver(create_driver("en-US")).closed_captions_language_is_already_selected("es-ES"))

    def test_returns_false_when_meeting_language_list_is_not_found(self):
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
