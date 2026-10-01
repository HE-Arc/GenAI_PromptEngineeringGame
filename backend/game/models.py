import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from game.quiz import QUIZ_SECONDS


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
    # WHICH didacticiel question the room is on, 0-based, and when its answering window closes.
    #
    # BOTH ARE ROOM STATE, not player state, and that is the whole change. The question on screen
    # belongs to the ROOM now: one question, one deadline, one reveal, for every phone in the
    # class. There is no per-player cursor left to disagree with another one.
    #
    # `quiz_deadline` is NULL when no window is open — a room waiting in the lobby, or one the
    # admin has just relaunched — and NULL means NO WINDOW rather than an expired one, so a client
    # can tell « nobody has asked anything yet » from « too late », which a deadline of zero could
    # not.
    #
    # A DATETIME rather than a flag or a counter, because the reveal is a COMPARISON — `now >=
    # deadline`, evaluated when the snapshot is built — and that is what lets the server run no
    # timer thread at all: the window is thirty seconds of shared WAITING, and the SSE stream
    # already rebuilds the whole snapshot twice a second, so the reveal reaches every phone
    # within half a second of the deadline.
    quiz_index = models.IntegerField(default=0)
    quiz_deadline = models.DateTimeField(null=True, blank=True)

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

    def _open_quiz(self, index: int = 0) -> None:
        """Put question `index` on screen and open its answering window.

        The window is opened HERE, by whoever put the question on screen, and nowhere else:
        `admin_start` opens the first one, the admin's advance button opens each of the next, and
        so the two paths cannot write different deadlines. It is deliberately NOT closed by the
        player who answered, and NOT closed here when the question is skipped: the admin pressing
        the button early moves the room on to the next question and the one it left is simply no
        longer on screen, which is the whole of the hurry-up path (§2).

        `save(update_fields=...)` and not a bare `save()`: a bare save from a half-loaded instance
        is how a phase change rewrites a field nobody meant to touch, and the two phase writers in
        this file keep their own two fields.
        """
        self.quiz_index = index
        self.quiz_deadline = timezone.now() + timedelta(seconds=QUIZ_SECONDS)
        self.save(update_fields=["quiz_index", "quiz_deadline"])

    def _next_quiz(self) -> bool:
        """Open the next question's window. False when the bank is behind the cursor.

        FALSE IS THE EIGHTH REVEAL BEING OVER, and it is the caller's cue to open the game: §2 says
        « le didacticiel bloque l'écriture des prompts, mais il ne se termine jamais tout seul », so
        nothing here reads a player's answer. It is the same button that opened question 0.

        The count is `self.questions`, THIS room's bank, for the reason every other count in this
        codebase is room-scoped: a second room's eight questions must not end this room's
        didacticiel early, and an unmaterialised bank (count 0) must not keep it shut forever —
        it falls straight through to opening the game, which is the only sensible reading of a
        gate with nothing in it.
        """
        nxt = self.quiz_index + 1
        if nxt >= self.questions.count():
            return False
        self._open_quiz(nxt)
        return True

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
    # All four of the settlement columns are NULL until the round is settled, and that is what
    # makes an in-progress round invisible to the ranking: `Sum` skips NULL, so a player with one
    # settled round and one open reads the settled total rather than a zero for the round being
    # played.
    #
    # `quality_llm` and `quality_comment` are the exceptions to "written at settlement", and they
    # have to be: they are `llm.grade_prompt` on the prompt alone, and the critic runs at ANSWER
    # time rather than at settlement time. `settle_round` is called by the last player's rating
    # tap, and making N billed calls from there is one HTTP request blocking for the better part
    # of a minute, on the one phase nobody is watching a list for. Every player pays for their own
    # call in parallel instead.
    quality_llm = models.IntegerField(null=True, blank=True)  # the critic, on the prompt alone
    # THE CRITIC'S REASON, in the same shape as `comment` above: stored with the score it justifies,
    # because the score alone cannot say whether a model produced it.
    #
    # It was written and thrown away, on the reasoning that the summary's four numbers told the
    # story. They do not: with no API key the critic is `_stub_grade`, a count of keyword markers,
    # and its score arrives on the summary labelled « note du modèle » with nothing on screen to
    # contradict that. The spec's hors-ligne rule asks for the opposite — « le modèle devient un
    # écho, le jury devient un chevauchement de mots, et l'écran le dit explicitement » — and this
    # is the string the screen reads to say it. `blank=True` and no `null=True`: a critic that
    # answered with a bare score has still said something, and llm.grade_prompt falls back to its
    # honesty string rather than to `""`, so an empty comment means a row nobody wrote it on.
    quality_comment = models.TextField(blank=True)
    # The peers' raw mean, BEFORE the band. Kept next to the bounded number so the summary can
    # show the bound at work rather than an unexplained verdict, and kept raw because a stored
    # 7 that used to be a 10 is the whole pedagogy of the feature.
    quality_peers = models.IntegerField(null=True, blank=True)
    # `prompts.quality(quality_llm, peers)`: the peers' mean held inside ±QUALITY_BAND of the
    # critic. What the ranking actually uses.
    quality = models.IntegerField(null=True, blank=True)
    # `(score + quality) / 2` — §7 of the spec, EXACTLY: "la note d'un tour est la moyenne des
    # deux dimensions". A float, not an integer, because a mean of two integers that do not
    # divide evenly has to BE one: floor-dividing it was the implementation's own idea, and it
    # cost every game up to half a point per round off a scale the final screen still calls
    # "sur 100" — ten rounds of proximity 7 and quality 6 settled to 60 where the spec's
    # arithmetic gives 65. The two halves are still integers, so only this column is a float, and
    # the payload carries `6.5` where a player has to be able to read the division they were
    # taught.
    round_score = models.FloatField(null=True, blank=True)

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
