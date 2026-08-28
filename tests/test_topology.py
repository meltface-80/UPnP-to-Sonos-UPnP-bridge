from sonosbridge.sonos import parse_zone_group_state

MODERN = """<ZoneGroupState><ZoneGroups>
<ZoneGroup Coordinator="RINCON_AAA01400" ID="RINCON_AAA01400:12">
  <ZoneGroupMember UUID="RINCON_AAA01400" ZoneName="Kitchen"
    Location="http://192.168.1.10:1400/xml/device_description.xml" SoftwareVersion="70.3"/>
  <ZoneGroupMember UUID="RINCON_SUB01400" ZoneName="Kitchen" Invisible="1"
    Location="http://192.168.1.11:1400/xml/device_description.xml"
    ChannelMapSet="RINCON_SUB01400:SW,SW"/>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_EEE01400" ID="RINCON_EEE01400:4">
  <ZoneGroupMember UUID="RINCON_EEE01400" ZoneName="Lounge"
    Location="http://192.168.1.20:1400/xml/device_description.xml"
    ChannelMapSet="RINCON_EEE01400:LF,LF;RINCON_FFF01400:RF,RF"/>
  <ZoneGroupMember UUID="RINCON_FFF01400" ZoneName="Lounge" Invisible="1"
    Location="http://192.168.1.21:1400/xml/device_description.xml"
    ChannelMapSet="RINCON_EEE01400:LF,LF;RINCON_FFF01400:RF,RF"/>
</ZoneGroup>
<ZoneGroup Coordinator="RINCON_CCC01400" ID="RINCON_CCC01400:9">
  <ZoneGroupMember UUID="RINCON_CCC01400" ZoneName="Study"
    Location="http://192.168.1.12:1400/xml/device_description.xml"/>
  <ZoneGroupMember UUID="RINCON_DDD01400" ZoneName="Bedroom"
    Location="http://192.168.1.13:1400/xml/device_description.xml"/>
  <ZoneGroupMember UUID="RINCON_BST01400" ZoneName="BOOST" IsZoneBridge="1"
    Location="http://192.168.1.14:1400/xml/device_description.xml"/>
</ZoneGroup>
</ZoneGroups><VanishedDevices/></ZoneGroupState>"""

LEGACY = """<ZoneGroups>
<ZoneGroup Coordinator="RINCON_AAA01400" ID="RINCON_AAA01400:1">
  <ZoneGroupMember UUID="RINCON_AAA01400" ZoneName="Lounge"
    Location="http://10.0.0.9:1400/xml/device_description.xml"/>
</ZoneGroup></ZoneGroups>"""


def test_parses_rooms_addresses_and_coordinators():
    zones = {z.uid: z for z in parse_zone_group_state(MODERN)}
    assert zones["RINCON_AAA01400"].name == "Kitchen"
    assert zones["RINCON_AAA01400"].ip == "192.168.1.10"
    assert zones["RINCON_AAA01400"].is_coordinator
    assert not zones["RINCON_DDD01400"].is_coordinator
    assert zones["RINCON_DDD01400"].coordinator_uid == "RINCON_CCC01400"


def test_subs_satellites_and_boosts_are_not_playable_rooms():
    zones = {z.uid: z for z in parse_zone_group_state(MODERN)}
    assert not zones["RINCON_SUB01400"].playable  # bonded sub
    assert not zones["RINCON_BST01400"].playable  # BOOST/BRIDGE
    assert zones["RINCON_AAA01400"].playable
    assert zones["RINCON_DDD01400"].playable  # grouped, but still its own room


def test_legacy_firmware_layout_is_accepted():
    zones = parse_zone_group_state(LEGACY)
    assert [z.name for z in zones] == ["Lounge"]


def test_garbage_input_is_survivable():
    assert parse_zone_group_state("") == []
    assert parse_zone_group_state("<not-xml") == []


def test_a_bonded_stereo_pair_is_one_room_that_knows_it_is_a_pair():
    zones = {z.uid: z for z in parse_zone_group_state(MODERN)}
    lounge = zones["RINCON_EEE01400"]
    assert lounge.playable
    assert lounge.stereo_pair
    assert not zones["RINCON_FFF01400"].playable  # the right-hand speaker is not a room
    assert not zones["RINCON_AAA01400"].stereo_pair  # a bonded sub is not a pair
