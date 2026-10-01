import secrets

from django.core.exceptions import ValidationError
from django.db import models


def _token() -> str:
    return secrets.token_urlsafe(12)


class Room(models.Model):
    # One field for where the game is, not a status plus a boolean: two fields describing the
    # same instant can contradict each other, and the SSE snapshot is the only place the phase
    # is read, so a contradiction there is invisible until a player is on the wrong screen.
    class Phase(models.TextChoices):
        WAITING = "waiting", "waiting"
        QUIZ = "quiz", "quiz"
        PLAYING = "playing", "playing"
        RATING = "rating", "rating"
        SUMMARY = "summary", "summary"
        DONE = "done", "done"

    code = models.CharField(max_length=8, primary_key=True)
    phase = models.CharField(max_length=8, choices=Phase.choices, default=Phase.WAITING)
    current = models.IntegerField(default=0)
    # Does this game open with the didacticiel? Fixed for the whole game, decided by the
    # admin at creation, so every player in the room faces the same gate. A restart carries
    # the flag: relaunching "without" means without, twice, not once.
    tutorial = models.BooleanField(default=True)
    # Anonymity during cross-rating is a per-game choice, not a per-round setting: flipping
    # it mid-game would tell players whose name appeared and whose did not, which leaks the
    # mapping the anonymity exists to hide.
    anonymous = models.BooleanField(default=True)

    def current_round(self):
        """The round being played, or None before the game starts. None is a real answer: in
        `waiting` there are no rounds yet, and every caller has to survive that."""
        return self.rounds.filter(order=self.current).first()

    def _go(self, phase: Phase) -> None:
        """Move the game to `phase`. The single write every transition goes through, so no two
        paths can disagree about what else has to move with the phase — a phase change that needs
        a round reset says so in the method that owns the transition, not here."""
        self.phase = phase
        self.save(update_fields=["phase"])

    def _open_rating(self) -> None:
        """Close the writing phase and put the first prompt of the round on screen.

        `revealed` is reset HERE rather than in `next_round`, because opening the rating is the
        only thing that decides which prompt is first; and it is per round, so a relaunched game
        cannot open on prompt 7 of a round it just deleted.
        """
        rnd = self.current_round()
        rnd.revealed = 0
        rnd.save(update_fields=["revealed"])
        self._go(self.Phase.RATING)

    def _advance_reveal(self) -> None:
        """Next prompt, or the summary once the last one is behind us.

        The cursor is an index into the round's answers ordered by id, so the count is the same
        ordering the reveal reads in: a round where nobody answered has no prompt to reveal, and
        the first advance lands on the summary rather than on an empty slide.
        """
        rnd = self.current_round()
        if rnd.revealed + 1 < rnd.answers.count():
            rnd.revealed += 1
            rnd.save(update_fields=["revealed"])
        else:
            self._go(self.Phase.SUMMARY)

    def next_round(self) -> None:
        """Advance one round, or finish the game. No-op at the last round."""
        if self.current + 1 < self.rounds.count():
            self.current += 1
            self.phase = self.Phase.PLAYING
        else:
            self.phase = self.Phase.DONE
        self.save(update_fields=["current", "phase"])


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
    # Which of this round's prompts is on screen. An index into the round's answers ordered
    # by id, NOT a player id: the reveal order is the order the answers landed, which is
    # join order, and a rating is a rate against a specific revealed line.
    revealed = models.IntegerField(default=0)

    class Meta:
        unique_together = [("room", "order")]


class Player(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="players")
    token = models.CharField(max_length=32, unique=True, default=_token)
    name = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("room", "name")]

    def quiz_score(self) -> int:
        """How many didacticiel questions this player got right. An integer, never None: the
        snapshot formats it as "3 sur 8" and the frontend should not carry a null branch that
        only a player who never answered would ever take. Deliberately NOT part of the final
        ranking — a gate you must pass must not also be points you can farm."""
        return self.quiz_attempts.filter(correct=True).count()


class Answer(models.Model):
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="answers")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="answers")
    prompt = models.TextField()
    output = models.TextField()
    # The judge's proximity to the target, 0-10. The first of the round's two dimensions.
    score = models.IntegerField()
    comment = models.TextField(blank=True)

    # The round's settlement: WRITTEN ONCE, NEVER RECOMPUTED. `views.settle_round` runs when the
    # round's rating closes and fills these in, and every later snapshot is a `SUM` over
    # `round_score` — the same shape the ranking already had.
    #
    # WHY A WRITE AND NOT A COMPUTATION AT READ TIME: in a class of twenty over ten rounds a
    # snapshot would have to sum two hundred answers and nearly four thousand peer ratings, twice
    # a second, for each of twenty-one streams (twenty phones and the admin). That is ~168 000
    # reads a second against a threaded `runserver` on a phone browser, on a classroom LAN. And
    # recomputing would be wrong in a second, worse way: a final ranking that moves while nobody
    # is playing is a score nobody can trust, and a peer note that landed late would rewrite the
    # nine rounds before it.
    #
    # All four are NULL until the round is settled, and that is what makes an in-progress round
    # invisible to the ranking: `Sum` skips NULL, so a player with one settled round and one open
    # reads the settled total rather than a zero for the round being played.
    #
    # `quality_llm` is the ONE exception to "written at settlement", and it has to be: it is
    # `llm.grade_prompt` on the prompt alone, and the critic runs at ANSWER time rather than at
    # settlement time. `settle_round` is called by the last player's rating tap, and making N
    # billed calls from there is one HTTP request blocking for the better part of a minute, on
    # the one phase nobody is watching a list for. Every player pays for their own call in
    # parallel instead.
    quality_llm = models.IntegerField(null=True, blank=True)  # the critic, on the prompt alone
    # The peers' raw mean, BEFORE the band. Kept next to the bounded number so the summary can
    # show the bound at work rather than an unexplained verdict, and kept raw because a stored
    # 7 that used to be a 10 is the whole pedagogy of the feature.
    quality_peers = models.IntegerField(null=True, blank=True)
    # `prompts.quality(quality_llm, peers)`: the peers' mean held inside ±QUALITY_BAND of the
    # critic. What the ranking actually uses.
    quality = models.IntegerField(null=True, blank=True)
    # `(score + quality) // 2` — the round's note out of ten, floor because the scale and the
    # payload are integers and a float in here would be a float in the JSON on every tick.
    round_score = models.IntegerField(null=True, blank=True)

    class Meta:
        unique_together = [("round", "player")]


class QuizQuestion(models.Model):
    # The didacticiel bank, materialised. Not named `Question`: `game.quiz.Question` is
    # already the NamedTuple of one hard-coded bank entry, and two `Question`s in adjacent
    # modules imported by the same `views.py` cannot be told apart at a glance — this is a row
    # in the table, that is a value in a file.

    # THE BANK BELONGS TO A ROOM. It did not used to: `order` was globally UNIQUE and there was
    # no room FK, which meant only ONE bank of eight could exist per database and a second
    # `admin_create_room` had to reuse it — two rooms in one install faced the same eight
    # questions in the same order, which is the opposite of what `quiz.pick()`'s docstring
    # promises. It also made the bank unrefreshable: `admin_create_room` materialised it once
    # behind `if not QuizQuestion.objects.exists()` and never again, so an admin who edited
    # `quiz.py` — the file the spec promises is editable without touching any code — saw no
    # change at all, not even after a restart. Owning the bank per room fixes both at once:
    # a new room draws a new order, and `admin_create_room` can materialise it fresh every time.
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="questions")

    # `order` is this ROOM's identity for a question, and it is unique within the room, because
    # the quiz endpoint addresses a question by `(room, order)`: two rows of one room sharing an
    # order would make "which question is this" ambiguous, and the ambiguity would surface as a
    # player being told their 3rd answer was right when the question they saw was the 4th. The
    # same order in two DIFFERENT rooms is meaningless rather than fatal — that is exactly the
    # variety `pick()` draws.
    order = models.IntegerField()
    prompt = models.TextField()
    # A JSONField, not a delimited text column: the four choices are read back whole, never
    # matched against, and no query ever asks "which questions have this choice". Storing them
    # as text would only add a parse that could disagree with the row it came from.
    choices = models.JSONField()
    # An index into `choices`, kept as the quiz bank does: rewriting a choice must not
    # invalidate the key, which a stored answer string would.
    correct = models.IntegerField()
    # Persisted, not read from `quiz.py` at answer time: the explanation belongs to the exact
    # version of the question the player was shown, and it travels in the response that reveals
    # the answer.
    explanation = models.TextField()

    class Meta:
        unique_together = [("room", "order")]


class QuizAttempt(models.Model):
    # `correct` is stored rather than derived from `question.correct` at read time, so the score
    # a player got is the score they were shown. Recomputing it later would silently rescore
    # every past game the day the bank is edited.
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="quiz_attempts")
    question = models.ForeignKey(QuizQuestion, on_delete=models.CASCADE, related_name="attempts")
    choice = models.IntegerField()
    correct = models.BooleanField()

    class Meta:
        # One attempt per question, enforced in the database: the client sends an answer the
        # moment a button is tapped and a retry on a flaky phone is indistinguishable from a
        # second answer. `unique_together` is the source of truth, not a check in the view.
        unique_together = [("player", "question")]


class Rating(models.Model):
    # One peer score per revealed prompt per rater. CASCADE all the way: a restart deletes the
    # rounds, and the ratings must go with them — a rating whose prompt no longer exists has no
    # row to be drawn next to.
    answer = models.ForeignKey(Answer, on_delete=models.CASCADE, related_name="ratings")
    rater = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="ratings_given")
    score = models.IntegerField()

    class Meta:
        # One rating per rater per prompt, in the database. The client cannot know which
        # prompt it is rating — the phase decides that — so this row is the only place the
        # rule can live, and it is what `rate` catches to answer 409.
        unique_together = [("answer", "rater")]
        constraints = [
            # 1-10 is the scale the whole rating phase is drawn against: the visual scale, the
            # visual summary and the ±3 band around the critic all assume it. A score outside it
            # is not a display problem, it is a wrong ranking produced silently.
            models.CheckConstraint(condition=models.Q(score__gte=1, score__lte=10), name="rating_score_1_10"),
        ]

    def save(self, *args, **kwargs) -> None:
        """Refuse a self-rating. `ValidationError`, not `IntegrityError`: unlike the score range
        above, this rule is not in the database at all."""
        # NO-ONE-RATES-THEIR-OWN-PROMPT IS NOT A DATABASE CONSTRAINT. It looks like one and
        # the plan used to ask for it as one — `~models.Q(answer__player=models.F("rater"))`
        # — which Django rejects at import with `models.E041: 'constraints' refers to the
        # joined field`. A CheckConstraint is compiled to SQL against THIS table, and
        # `answer__player` is a JOIN through Answer. There is no way to write it.
        #
        # So the rule is enforced where it can be: the view refuses it, because the view
        # derives the target from the reveal and the client never names it, which makes that
        # check unforgeable; and this guard is the backstop for a path that does not go
        # through the view — a future management command, a shell, a data fix.
        #
        # The guard reads the author through `self.answer`, so it costs one query and only on
        # create; it raises before any write, so it leaves no broken transaction behind.
        if self.answer_id and self.rater_id and self.answer.player_id == self.rater_id:
            raise ValidationError("a player cannot rate their own prompt")
        super().save(*args, **kwargs)
