"""Unit tests for the dynamic bridge (no network required).

Run with:  python3 -m unittest bridge.tests.test_bridge
"""

import unittest

from ..credentials import (
    AuthMethod,
    generate_dynamic,
    generate_static,
    method_order,
)
from ..bridge import TvConnection

KNOWN_MAC = "56:b8:88:4e:f7:19"
KNOWN_TIME = 1766974704


class TestCredentials(unittest.TestCase):
    def test_dynamic_matches_logcat_reference(self):
        creds = generate_dynamic(KNOWN_MAC, "his", AuthMethod.MODERN, timestamp=KNOWN_TIME)
        self.assertEqual(creds.client_id, "56:b8:88:4e:f7:19$his$256DBF_vidaacommon_001")
        self.assertEqual(creds.username, "his$6239759786168176024")
        self.assertEqual(creds.password, "C3BA44782E18ABF4892AC44D79A622D2")

    def test_client_id_matches_the_description_mac(self):
        # The hash is derived from the MAC exactly as the TV computes it.
        a = generate_dynamic("AC:1F:6B:FA:63:30", "tve", AuthMethod.MODERN, timestamp=KNOWN_TIME)
        self.assertEqual(a.client_id, "AC:1F:6B:FA:63:30$tve$B248DF_vidaacommon_001")

    def test_dynamic_client_id_independent_of_method(self):
        for method in (AuthMethod.LEGACY, AuthMethod.MIDDLE, AuthMethod.MODERN):
            c = generate_dynamic(KNOWN_MAC, "his", method, timestamp=KNOWN_TIME)
            self.assertEqual(c.client_id, "56:b8:88:4e:f7:19$his$256DBF_vidaacommon_001")

    def test_static_uses_fixed_login(self):
        creds = generate_static("HomeAssistant")
        self.assertEqual(creds.client_id, "HomeAssistant")
        self.assertEqual(creds.username, "hisenseservice")
        self.assertEqual(creds.password, "multimqttservice")

    def test_method_order(self):
        self.assertEqual(
            [m.value for m in method_order(2140)],
            ["static", "legacy", "middle", "modern"],
        )
        self.assertEqual(
            [m.value for m in method_order(3280)],
            ["middle", "modern", "legacy", "static"],
        )
        self.assertEqual(
            [m.value for m in method_order(3290)],
            ["modern", "middle", "legacy", "static"],
        )
        self.assertEqual(
            [m.value for m in method_order(None)],
            ["modern", "middle", "legacy", "static"],
        )


def make_tv(method, prefix="hisense", ha_id="HomeAssistant"):
    tv = TvConnection({"host": "x"}, prefix, prefix, ha_id, lambda *_: None)
    tv._tv_topic_client_id = "AA:BB:CC:DD:EE:FF$his$ABCDEF_vidaacommon_001"
    tv._method = method
    return tv


class TestTopicMapping(unittest.TestCase):
    def test_ha_to_tv_dynamic(self):
        tv = make_tv(AuthMethod.MODERN)
        out = tv.ha_to_tv("hisense/remoteapp/tv/ui_service/HomeAssistant/actions/sendkey")
        self.assertEqual(
            out,
            "/remoteapp/tv/ui_service/AA:BB:CC:DD:EE:FF$his$ABCDEF_vidaacommon_001/actions/sendkey",
        )

    def test_tv_to_ha_dynamic(self):
        tv = make_tv(AuthMethod.MODERN)
        out = tv._tv_to_ha(
            "/remoteapp/mobile/AA:BB:CC:DD:EE:FF$his$ABCDEF_vidaacommon_001/ui_service/data/vidaa_app_connect"
        )
        self.assertEqual(out, "hisense/remoteapp/mobile/HomeAssistant/ui_service/data/vidaa_app_connect")

    def test_broadcast_untouched(self):
        tv = make_tv(AuthMethod.MODERN)
        self.assertEqual(
            tv._tv_to_ha("/remoteapp/mobile/broadcast/ui_service/state"),
            "hisense/remoteapp/mobile/broadcast/ui_service/state",
        )

    def test_no_rewrite_for_static(self):
        tv = make_tv(AuthMethod.STATIC, prefix="my/tv")
        tv._tv_topic_client_id = "HomeAssistant"
        self.assertEqual(
            tv.ha_to_tv("my/tv/remoteapp/tv/ui_service/HomeAssistant/actions/sendkey"),
            "/remoteapp/tv/ui_service/HomeAssistant/actions/sendkey",
        )
        self.assertEqual(
            tv._tv_to_ha("/remoteapp/mobile/HomeAssistant/ui_service/data/state"),
            "my/tv/remoteapp/mobile/HomeAssistant/ui_service/data/state",
        )

    def test_subscribe_topics_are_exact(self):
        from ..bridge import TV_SUBSCRIBE_TOPICS

        self.assertTrue(TV_SUBSCRIBE_TOPICS)
        for template in TV_SUBSCRIBE_TOPICS:
            self.assertNotIn("#", template, f"wildcard not allowed: {template}")
            self.assertNotIn("+", template, f"wildcard not allowed: {template}")
            cid = "AA:BB:CC:DD:EE:FF$his$ABCDEF_vidaacommon_001"
            if "/broadcast/" in template:
                self.assertNotIn("{cid}", template)
                formatted = template
            else:
                self.assertIn("{cid}", template)
                formatted = template.format(cid=cid)
            self.assertTrue(formatted.startswith("/remoteapp/"))