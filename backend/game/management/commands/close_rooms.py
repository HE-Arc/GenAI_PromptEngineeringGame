"""`manage.py close_rooms`: delete every room, so a fresh server starts with no game open.

Run by `run.sh` / `run.bat` just before `runserver`. Closing the games at START rather than at
shutdown is deliberate: a Ctrl+C or a closed window gives the server no time to clean up, while
the next start always comes. It is a separate command and not `AppConfig.ready()` so that the
autoreloader restarting the server on a code edit does not wipe a game being played.
"""

from django.core.management.base import BaseCommand

from game.models import Room


class Command(BaseCommand):
    help = "Delete every room (and, by cascade, its players, rounds and answers)."

    def handle(self, *args, **options) -> None:
        count = Room.objects.count()
        # One delete for the table: the cascade takes the players, the rounds, the answers, the
        # ratings, the votes and the quiz banks with their rooms.
        Room.objects.all().delete()
        self.stdout.write(f"{count} partie(s) fermee(s).")
