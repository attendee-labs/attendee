from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from bots.teams_bot_adapter import TeamsBotAdapter
from bots.webex_bot_adapter import WebexBotAdapter
from bots.webex_bot_adapter.webex_bot_adapter import WEBEX_DOMAIN_ALLOWLIST
from bots.zoom_web_bot_adapter import ZoomWebBotAdapter

# The hosts a Webex bot contacted while joining a meeting, taking part in it and receiving captions, as seen in Chrome's network log
HOSTS_CONTACTED_BY_A_WEBEX_BOT = [
    "unpkg.com",
    "127.0.0.1",
    "api.ciscospark.com",
    "calliope-anycast.prod.infra.webex.com",
    "data-channel-r.wbx2.com",
    "ds.ciscospark.com",
    "llm1-connection-r.wbx2.com",
    "locus-r.wbx2.com",
    "mercury-connection-partition0-r.wbx2.com",
    "nebulab.webex.com",
    "tsa.webex.com",
    "u2c-r.wbx2.com",
    "u2c.wbx2.com",
    "wdm-r.wbx2.com",
]


class WebexDomainAllowlistTest(SimpleTestCase):
    def setUp(self):
        self.adapter = object.__new__(WebexBotAdapter)  # none of these tests need the adapter's constructor

    def test_every_host_a_webex_bot_contacts_is_allowed(self):
        for host in HOSTS_CONTACTED_BY_A_WEBEX_BOT:
            with self.subTest(host=host):
                self.assertFalse(self.adapter.url_violates_domain_allow_list(f"https://{host}/"))

    def test_other_hosts_are_not_allowed(self):
        for host in ("example.com", "webex.com.example.com", "notwebex.com", "evilwbx2.com"):
            with self.subTest(host=host):
                self.assertTrue(self.adapter.url_violates_domain_allow_list(f"https://{host}/"))

    def test_the_allowlist_cannot_be_changed_through_the_adapter(self):
        self.adapter.subclass_specific_domain_allowlist().append("example.com")

        self.assertNotIn("example.com", WEBEX_DOMAIN_ALLOWLIST)


class ChromePoliciesTest(SimpleTestCase):
    def test_chrome_blocks_everything_but_the_allowlist_when_it_is_enforced(self):
        for adapter_class in (WebexBotAdapter, ZoomWebBotAdapter, TeamsBotAdapter):
            with self.subTest(adapter=adapter_class.__name__), override_settings(ENFORCE_DOMAIN_ALLOWLIST_IN_CHROME=True):
                adapter = object.__new__(adapter_class)

                with patch.object(adapter_class, "subclass_specific_domain_allowlist", return_value=["allowed.example.com"]):
                    self.assertEqual(adapter.subclass_specific_chrome_policies(), {"URLBlocklist": ["*"], "URLAllowlist": ["allowed.example.com"]})

    def test_chrome_has_no_policies_when_the_allowlist_is_not_enforced(self):
        for adapter_class in (WebexBotAdapter, ZoomWebBotAdapter, TeamsBotAdapter):
            with self.subTest(adapter=adapter_class.__name__), override_settings(ENFORCE_DOMAIN_ALLOWLIST_IN_CHROME=False):
                self.assertEqual(object.__new__(adapter_class).subclass_specific_chrome_policies(), {})
