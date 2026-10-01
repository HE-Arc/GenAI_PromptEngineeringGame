"""Bank the didacticiel per room: `QuizQuestion` gains a room, `order` becomes per-room.

The defect this fixes is the schema, not the view. `QuizQuestion.order` was globally UNIQUE
and there was no room FK, so only one bank of eight could exist per database and a second
`admin_create_room` had to reuse it — two rooms in one install faced the same eight questions
in the same order, the opposite of what `quiz.pick()`'s docstring promises. It also made the
bank unrefreshable: it was materialised once behind `if not QuizQuestion.objects.exists()` and
never again, so an admin who edited `quiz.py` saw no change, not even on a new room.

Adding the room is forced to be NOT NULL, and that is the whole reason this migration is
written by hand rather than accepted from `makemigrations`. Autodetect emitted the FK with a
one-off `default=None` + `preserve_default=False`, which on a database that already has rows
fails at the ALTER with an integrity error — the prompt Django shows is asking the developer to
guess a room to hang a room-less bank on, and inventing one is worse than losing it. So the FK
goes in NULLABLE, the stale rows are DELETED, and only then is it tightened.

WHY DELETING IS THE HONEST ANSWER. Under the new schema a bank belongs to a room; an existing
bank belongs to no room, so there is no correct value for the column. Every `QuizAttempt` that
points at one of those rows is about that exact set of eight in that exact order and is
meaningless the moment a room draws its own, so the attempts are not being orphaned by the
delete — they were already orphaned, and `QuizAttempt.question` CASCADE takes them with it.
A developer's `db.sqlite3` is disposable and `admin_create_room` re-materialises a correct bank
for every room it makes from now on. Inventing a room to hang the rows on would instead
fabricate a game nobody played, with questions those players were never shown.
"""

import django.db.models.deletion
from django.db import migrations, models


def _delete_room_less_bank(apps, schema_editor):
    """Drop the rows that cannot be attributed to a room, before the column stops being nullable.

    Reverse is a no-op on purpose: the deleted rows cannot be reconstructed — a `RunPython` that
    re-inserted empty shells would put a room-less bank back, which is the state this migration
    exists to leave. Reversing is only meaningful for a fresh database, and there it deletes
    nothing.
    """
    QuizQuestion = apps.get_model("game", "QuizQuestion")
    QuizQuestion.objects.filter(room__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0003_quiz_bank_ratings_and_room_phase'),
    ]

    operations = [
        # First: drop the GLOBAL unique on `order`. Until this runs, a per-room schema is not
        # expressible — a second room cannot own an `order` the first one already has. There is
        # nothing to renumber, because the per-room rows are created from `enumerate()` at room
        # creation and are 0..n-1 by construction.
        migrations.AlterField(
            model_name='quizquestion',
            name='order',
            field=models.IntegerField(),
        ),
        # Then the FK, NULLABLE and with no default. `preserve_default=False` is what stops the
        # `default=None` the autodetector would have written from being kept in the model state
        # forever: it is a transition artefact, not a real default, and leaving it in place would
        # mean the *model* silently accepted a null room even though the column does not.
        migrations.AddField(
            model_name='quizquestion',
            name='room',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='questions',
                to='game.room',
            ),
            preserve_default=False,
        ),
        migrations.RunPython(_delete_room_less_bank, migrations.RunPython.noop),
        # Only now is NOT NULL safe: there is no row left for it to reject.
        migrations.AlterField(
            model_name='quizquestion',
            name='room',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='questions',
                to='game.room',
            ),
        ),
        # Scoped uniqueness last, because on SQLite it is enforced by a unique index that cannot
        # be built while two rooms' rows would still be colliding on the old global unique.
        migrations.AlterUniqueTogether(
            name='quizquestion',
            unique_together={('room', 'order')},
        ),
    ]