import secrets

from django.db import models


def _token() -> str:
    return secrets.token_urlsafe(12)


class Room(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "waiting"
        PLAYING = "playing", "playing"
        DONE = "done", "done"

    code = models.CharField(max_length=8, primary_key=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.WAITING)
    current = models.IntegerField(default=0)

    def next_round(self) -> None:
        """Advance one round, or finish the game. No-op at the last round."""
        if self.current + 1 < self.rounds.count():
            self.current += 1
            self.status = self.Status.PLAYING
        else:
            self.status = self.Status.DONE
        self.save(update_fields=["current", "status"])


class Round(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="rounds")
    order = models.IntegerField()
    # Targets are long-form text now — a recipe, a description, a riddle — so 200 characters
    # no longer holds one. 1000 leaves about 3x the longest entry in `targets.py` and room for
    # the list to be edited without a second migration.
    #
    # WHY THIS WAS NEVER CAUGHT: `views.admin_start` writes rounds with `bulk_create`, which goes
    # straight to INSERT and never calls `full_clean()`. Django's `max_length` is a validation
    # rule, not a database constraint, so an over-long target used to be accepted silently — the
    # column is `varchar` and SQLite does not enforce the length. A 300-character target would
    # have been stored fine and nobody would have found out from this line.
    target = models.CharField(max_length=1000)

    class Meta:
        unique_together = [("room", "order")]


class Player(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="players")
    token = models.CharField(max_length=32, unique=True, default=_token)
    name = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("room", "name")]


class Answer(models.Model):
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="answers")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="answers")
    prompt = models.TextField()
    output = models.TextField()
    score = models.IntegerField()
    comment = models.TextField(blank=True)

    class Meta:
        unique_together = [("round", "player")]
