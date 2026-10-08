from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from game.models import Player, Room


class CloseRoomsTests(TestCase):
    def test_every_room_and_its_players_are_deleted(self):
        for code in ("AAAA", "BBBB"):
            room = Room.objects.create(code=code)
            Player.objects.create(room=room, name="ana")
        out = StringIO()
        call_command("close_rooms", stdout=out)
        self.assertFalse(Room.objects.exists())
        self.assertFalse(Player.objects.exists())
        self.assertIn("2 partie(s) fermee(s).", out.getvalue())

    def test_an_empty_database_is_fine(self):
        call_command("close_rooms", stdout=StringIO())
        self.assertFalse(Room.objects.exists())
