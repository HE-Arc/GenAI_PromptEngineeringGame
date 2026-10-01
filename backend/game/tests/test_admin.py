import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from game.llm import Verdict
from game.models import Answer, Player, QuizAttempt, QuizQuestion, Rating, Room, Round
from game.quiz import QUIZ_LENGTH, QUIZ_SECONDS
from game.views import room_state


@override_settings(ADMIN_PASSWORD="s3cret")
class AdminTest(TestCase):
    # This module is also the regression guard for Task 1's MIDDLEWARE: `admin_login` writes
    # `request.session["admin"]`, which raises AttributeError without `SessionMiddleware` in the
    # chain. If every test here 500s, check that middleware before anything else.
    def setUp(self) -> None:
        self.c = Client()
        self._login()

    def _login(self, pwd: str = "s3cret") -> HttpResponse:
        return self.c.post("/api/admin/login", data=json.dumps({"password": pwd}), content_type="application/json")

    def _post(self, url: str, data: dict | None = None) -> HttpResponse:
        """A POST with a JSON body, or with none at all — the admin views read no body, and
        `body()` turns an empty one into `{}` rather than a parse error."""
        return self.c.post(url, data=json.dumps(data or {}), content_type="application/json")

    # --- fixtures for the phase machine -----------------------------------------------------
    #
    # Built through the ORM on purpose wherever a test is about "one advance from THIS phase":
    # a fixture that walked there through `start` + `next` would test the transitions twice and
    # fail for reasons that have nothing to do with the assertion in front of it.

    def _room(self, code: str = "ABCD", tutorial: bool = True, anonymous: bool = True) -> Room:
        return Room.objects.create(code=code, tutorial=tutorial, anonymous=anonymous)

    def _started(self, code: str = "ABCD", tutorial: bool = False) -> Room:
        """A room the admin has committed to: ten rounds, and whatever phase `start` opens for the
        given tutorial flag."""
        room = self._room(code, tutorial=tutorial)
        self._post(f"/api/admin/rooms/{code}/start")
        room.refresh_from_db()
        return room

    def _in_phase(
        self,
        phase: Room.Phase,
        answers: int = 0,
        *,
        revealed: int = 0,
        rounds: int = 2,
        code: str = "ABCD",
        tutorial: bool = False,
        anonymous: bool = True,
    ) -> Room:
        """A room parked in `phase`, on round 0, with `answers` answers already on it.

        `revealed` is the round's reveal cursor, passed explicitly because "the last prompt of
        three" and "the first prompt of three" are different starting points and neither is more
        canonical than the other. Two rounds by default, so `next_round()` has somewhere to go:
        with a single round it goes straight to `done` and every summary test would be testing
        the end of the game instead of the summary.
        """
        room = self._room(code, tutorial=tutorial, anonymous=anonymous)
        for order in range(rounds):
            rnd = Round.objects.create(room=room, order=order, target=f"cible {order}")
        rnd = room.rounds.get(order=0)
        rnd.revealed = revealed
        rnd.save(update_fields=["revealed"])
        for i in range(answers):
            rnd.answers.create(
                player=Player.objects.create(room=room, name=f"p{i}"),
                prompt=f"prompt {i}",
                output=f"reponse {i}",
                score=5,
            )
        room.current = 0
        room.phase = phase
        room.save(update_fields=["current", "phase"])
        return room

    def _next(self, room: Room) -> HttpResponse:
        return self._post(f"/api/admin/rooms/{room.code}/next")

    def _bank(self, room: Room) -> None:
        """Materialise this room's eight questions, as `admin_create_room` does.

        ON THE ORM and not through `POST /api/admin/rooms`, because every fixture in this module is
        built on the ORM and the didacticiel tests below are about what the advance button does to
        the CURSOR — not about where the questions come from, which `test_quiz` covers end to end.
        The keys are spread over the four choices so a test cannot pass by accident on a question
        whose answer happens to be choice 0.
        """
        QuizQuestion.objects.bulk_create(
            [
                QuizQuestion(
                    room=room,
                    order=i,
                    prompt=f"question {i}",
                    choices=["un", "deux", "trois", "quatre"],
                    correct=i % 4,
                    explanation=f"parce que {i}",
                )
                for i in range(QUIZ_LENGTH)
            ]
        )

    def _quiz_room(self, code: str = "ABCD") -> Room:
        """A started tutorial room with a real bank, sitting on question 0 with its window open —
        the state `admin_start` is supposed to leave behind."""
        room = self._room(code, tutorial=True)
        self._bank(room)
        self._post(f"/api/admin/rooms/{code}/start")
        room.refresh_from_db()
        return room

    def test_login_rejects_bad_password(self) -> None:
        self.assertEqual(self._login("nope").status_code, 403)

    def test_login_accepts_good_password(self) -> None:
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True})
        # Do not delete this line as redundant. It is the only regression guard for Task 1's
        # `SessionMiddleware`: without that middleware `admin_login` raises on the session write
        # (caught above) and `process_response` never sets the cookie (caught here).
        self.assertIn("sessionid", self.c.cookies)

    def test_create_room_requires_login(self) -> None:
        self.c = Client()
        self.assertEqual(self.c.post("/api/admin/rooms", content_type="application/json").status_code, 403)

    def test_create_room_returns_unique_code(self) -> None:
        a = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)
        b = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)
        self.assertNotEqual(a["code"], b["code"])
        self.assertEqual(len(a["code"]), 4)

    def test_start_requires_login(self) -> None:
        # Both halves of the auth spine, in one test: `require_admin` on `admin_start` and on
        # `admin_next`. Dropping either leaves every other test here green.
        Room.objects.create(code="WXYZ")
        c = Client()
        self.assertEqual(c.post("/api/admin/rooms/WXYZ/start", content_type="application/json").status_code, 403)
        self.assertEqual(Round.objects.count(), 0)
        self.assertEqual(c.post("/api/admin/rooms/WXYZ/next", content_type="application/json").status_code, 403)
        self.assertEqual(Room.objects.get(code="WXYZ").phase, "waiting")

    def test_start_creates_ten_rounds(self) -> None:
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        r = self._post(f"/api/admin/rooms/{code}/start")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["total"], 10)
        self.assertEqual(Round.objects.filter(room__code=code).count(), 10)

    def test_long_target_survives_start_and_appears_whole_in_room_state(self) -> None:
        # The round trip, end to end: `admin_start` picks the target, writes it, and `room_state`
        # hands the current one to every phone. A long target has to come out of all three
        # unchanged, because `room_state`'s "target" is what the player is playing against.
        #
        # This was never caught by a test because it was never caught at all: `admin_start`
        # writes with `bulk_create`, which skips `full_clean()`, and `max_length` is a
        # validation rule rather than a database constraint. An over-long target used to be
        # stored silently. So the assertion here is on the exact string, not on the length
        # being "reasonable" — a truncation is what this is guarding.
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        long_target = "la recette " + "et encore un detail " * 40 + "de fin"
        # Patched where it is USED, not where it is defined: `views` does
        # `from game.targets import pick`, so the name in this module's namespace is the one
        # `admin_start` calls. Patching `game.targets.pick` would leave the real bank in play
        # and this test would silently assert against whichever target got drawn first.
        with patch("game.views.pick", return_value=[long_target] * 10):
            r = self._post(f"/api/admin/rooms/{code}/start")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Round.objects.get(room__code=code, order=0).target, long_target)
        self.assertEqual(room_state(code)["target"], long_target)

    def test_start_is_idempotent_refuses_second_call(self) -> None:
        code = json.loads(self.c.post("/api/admin/rooms", content_type="application/json").content)["code"]
        self._post(f"/api/admin/rooms/{code}/start")
        self.assertEqual(self._post(f"/api/admin/rooms/{code}/start").status_code, 409)

    # --- start chooses the opening phase ----------------------------------------------------

    def test_start_with_tutorial_opens_the_quiz(self) -> None:
        room = self._room()
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.QUIZ)

    def test_start_without_tutorial_goes_straight_to_playing(self) -> None:
        room = self._room(tutorial=False)
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)

    def test_rounds_are_created_at_start_even_with_the_quiz_open(self) -> None:
        # The ten targets are drawn once, at start, whoever is looking. Drawing them when the
        # quiz closes would make the quiz wait on a second event for no benefit.
        room = self._room()
        self._post(f"/api/admin/rooms/{room.code}/start")
        self.assertEqual(room.rounds.count(), 10)

    def test_the_two_flags_are_read_when_the_room_is_created(self) -> None:
        # Not an admin-transition test, but this is the only way the flag becomes reachable over
        # HTTP, and `admin_start`'s whole branch on `room.tutorial` is dead code without it: a
        # party could never be launched without the didacticiel, and a closed gate is unplayable.
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": False, "anonymous": False}).content)["code"]
        room = Room.objects.get(code=code)
        self.assertEqual((room.tutorial, room.anonymous), (False, False))
        # Absent means the model defaults, which is the safe direction: gate on, names hidden.
        code2 = json.loads(self._post("/api/admin/rooms").content)["code"]
        room2 = Room.objects.get(code=code2)
        self.assertEqual((room2.tutorial, room2.anonymous), (True, True))

    # --- one advance, dispatched per phase --------------------------------------------------

    def test_next_from_quiz_starts_the_game(self) -> None:
        # A room whose bank was never materialised has nowhere to walk: the cursor has no question
        # to move to, so the button opens the game rather than pressing a dead control. The banked
        # case — seven presses and then the game — is `test_the_eighth_advance_from_the_quiz_opens_
        # the_game` below, and this one pins only that it cannot hang.
        room = self._started(tutorial=True)
        self.assertEqual(room.phase, Room.Phase.QUIZ)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)

    # --- the advance button, in the quiz: one question at a time --------------------------------

    def test_start_opens_the_first_question_and_its_window(self) -> None:
        # THE ADMIN'S FIRST PRESS HAS TO PUT A QUESTION ON SCREEN. It used to open a phase and
        # leave the class on whatever their own client had decided to show, which was each
        # player's own cursor into the bank; now there is one cursor, on the room, and `start` is
        # the only thing that can put it at 0 — the button that opens question 1 has to walk from
        # somewhere, and a relaunch resets it to here.
        room = self._quiz_room()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        self.assertIsNotNone(room.quiz_deadline)
        seconds = (room.quiz_deadline - timezone.now()).total_seconds()
        self.assertLessEqual(seconds, QUIZ_SECONDS)
        self.assertGreater(seconds, QUIZ_SECONDS - 5)
        # And without a tutorial there is no question and no window: `start` must not open a clock
        # on a didacticiel this room is not playing. A second code, because the first room is still
        # in the database and the code is the primary key.
        without = self._started(code="NOTUT", tutorial=False)
        self.assertEqual(without.phase, Room.Phase.PLAYING)
        self.assertEqual(without.quiz_index, 0)
        self.assertIsNone(without.quiz_deadline)

    def test_next_from_quiz_opens_the_next_question_and_its_window(self) -> None:
        # ONE BUTTON, AND WHAT IT DOES ON THE WAY: move the room's cursor, open a FRESH fifteen
        # seconds on the new question, and leave the phase alone. The gate does not close itself
        # either — §2 gives the advance to a person for the same reason it gives the commentary of
        # a round to a person.
        room = self._quiz_room()
        before = room.quiz_deadline
        r = self._next(room)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "phase": "quiz"})
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 1))
        self.assertGreater(room.quiz_deadline, before)
        seconds = (room.quiz_deadline - timezone.now()).total_seconds()
        self.assertLessEqual(seconds, QUIZ_SECONDS)
        self.assertGreater(seconds, QUIZ_SECONDS - 5)

    def test_the_eighth_advance_from_the_quiz_opens_the_game(self) -> None:
        # « Après la huitième révélation, le même bouton ouvre la partie. » Seven presses walk
        # 0 -> 7 and the eighth has nowhere left in the bank, so it opens the writing phase. The
        # count is the whole claim: an implementation that opened the game on the first press, or
        # left the room in the quiz after the eighth, fails one of the two ends of this loop.
        room = self._quiz_room()
        for index in range(1, QUIZ_LENGTH):
            self._next(room)
            room.refresh_from_db()
            self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, index))
        r = self._next(room)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "phase": "playing"})
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        # The cursor stays on the eighth question rather than wrapping or resetting: only a restart
        # puts it back to 0, and a game that opened the writing phase has nothing more to ask.
        self.assertEqual(room.quiz_index, QUIZ_LENGTH - 1)

    def test_next_from_quiz_before_the_deadline_does_not_reveal_the_skipped_question(self) -> None:
        # THE HURRY-UP PATH. « Le même bouton referme la question en cours quand il est pressé avant
        # l'échéance, pour passer plus vite quand la classe a compris. » Two things are asserted: the
        # next question opens at once with its own window, and the one that was skipped is never
        # revealed. The second is not a formality — the key of question 0 exists in the database
        # forever, and if the reveal were written down at skip time rather than read off the
        # deadline of whatever is on screen, that key would be published to every phone in the room.
        room = self._quiz_room()
        quiz = room_state(room.code)["quiz"]
        self.assertFalse(quiz["revealed"])
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 1)
        # Question 1 is not revealed either, so the new window is genuinely fifteen seconds long.
        quiz = room_state(room.code)["quiz"]
        self.assertEqual(quiz["index"], 1)
        self.assertFalse(quiz["revealed"])
        self.assertIsNone(quiz["correct"])
        self.assertIsNone(quiz["explanation"])
        # And a skipped question leaves the room perfectly playable: the player simply has one
        # attempt fewer, and the quiz's own score is the sum of the ones they did make.
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_next_from_playing_opens_the_rating(self) -> None:
        room = self._started(tutorial=False)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.RATING)
        self.assertEqual(room.rounds.get(order=0).revealed, 0)

    def test_opening_the_rating_resets_a_stale_reveal_cursor(self) -> None:
        # `revealed` is per round, and a round that was rated once would otherwise open the
        # second rating on prompt 7 of a turn with three prompts.
        room = self._in_phase(Room.Phase.PLAYING, answers=3, revealed=2)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.RATING)
        self.assertEqual(room.rounds.get(order=0).revealed, 0)

    def test_next_from_rating_reveals_the_next_prompt(self) -> None:
        room = self._in_phase(Room.Phase.RATING, answers=2)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.RATING)
        self.assertEqual(room.rounds.get(order=0).revealed, 1)

    def test_next_from_rating_on_the_last_prompt_opens_the_summary(self) -> None:
        room = self._in_phase(Room.Phase.RATING, answers=3, revealed=2)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.SUMMARY)
        self.assertEqual(room.rounds.get(order=0).revealed, 2)  # the cursor does not run past the end

    def test_next_from_rating_with_no_answers_opens_the_summary(self) -> None:
        # Nobody wrote a prompt, so there is nothing to reveal: the round must not sit on an
        # empty slide waiting for an advance that could never be right.
        room = self._in_phase(Room.Phase.RATING)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.SUMMARY)

    def test_next_from_summary_starts_the_next_round(self) -> None:
        room = self._in_phase(Room.Phase.SUMMARY)
        self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertEqual(room.current, 1)

    def test_next_from_waiting_is_refused(self) -> None:
        room = self._room()
        self.assertEqual(self._next(room).status_code, 409)

    def test_next_from_done_is_refused(self) -> None:
        # The eleventh advance must be a 409, not an eleventh round: the game ends, it does not
        # roll over.
        room = self._in_phase(Room.Phase.DONE)
        self.assertEqual(self._next(room).status_code, 409)

    def test_next_answers_with_the_phase_it_landed_on(self) -> None:
        # The admin screen polls `room_state` and posts `next`; it must not have to guess which
        # of the six it just triggered.
        room = self._started(tutorial=False)
        self.assertEqual(json.loads(self._next(room).content), {"ok": True, "phase": "rating"})

    def test_twelve_rounds_walk_to_done(self) -> None:
        # A round is now several admin actions, not one. Ten rounds must still end at `done`,
        # and the eleventh advance must be a 409 rather than an eleventh round.
        room = self._started(tutorial=False)
        for _ in range(40):
            if room.phase == Room.Phase.DONE:
                break
            self._next(room)
            room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.DONE)
        self.assertEqual(self._next(room).status_code, 409)

    def test_a_tutorial_game_still_walks_to_done(self) -> None:
        # Same walk, one phase longer: the didacticiel is one advance on top of the ten rounds,
        # so it must not be the thing that pushes the game past `done`.
        room = self._started(tutorial=True)
        for _ in range(40):
            if room.phase == Room.Phase.DONE:
                break
            self._next(room)
            room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.DONE)
        self.assertEqual(room.current, 9)
        self.assertEqual(self._next(room).status_code, 409)

    # --- restart ---------------------------------------------------------------------------

    def test_restart_requires_login(self) -> None:
        Room.objects.create(code="WXYZ", phase=Room.Phase.PLAYING)
        c = Client()
        self.assertEqual(c.post("/api/admin/rooms/WXYZ/restart", content_type="application/json").status_code, 403)
        room = Room.objects.get(code="WXYZ")
        self.assertEqual(room.phase, "playing")  # nothing was touched

    def test_restart_from_playing_returns_to_lobby_and_keeps_players(self) -> None:
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": False}).content)["code"]
        self._join(code, "ana")
        self._post(f"/api/admin/rooms/{code}/start")
        r = self._post(f"/api/admin/rooms/{code}/restart")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "phase": "waiting"})
        room = Room.objects.get(code=code)
        self.assertEqual((room.phase, room.current), ("waiting", 0))
        self.assertEqual(Round.objects.filter(room=room).count(), 0)
        self.assertEqual([p.name for p in room.players.all()], ["ana"])

    def test_restart_from_done_returns_to_lobby(self) -> None:
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": False}).content)["code"]
        self._join(code, "ana")
        self._post(f"/api/admin/rooms/{code}/start")
        url = f"/api/admin/rooms/{code}/next"
        for _ in range(40):
            if self._post(url).status_code == 409:
                break
        self.assertEqual(Room.objects.get(code=code).phase, Room.Phase.DONE)
        r = self._post(f"/api/admin/rooms/{code}/restart")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "phase": "waiting"})
        room = Room.objects.get(code=code)
        self.assertEqual((room.phase, room.current), ("waiting", 0))
        self.assertEqual(Round.objects.filter(room=room).count(), 0)
        self.assertEqual(room.players.count(), 1)

    def test_restart_keeps_the_tutorial_and_anonymity_flags(self) -> None:
        # A checkbox that silently unticks itself on restart is a flag nobody can trust: the
        # second game would open its gate and name its authors without anybody deciding to.
        # WHICH is what a body without the key means: not "off", but "nobody said". The tests
        # below cover the half that changes it.
        room = self._in_phase(Room.Phase.SUMMARY, tutorial=False, anonymous=False)
        self._post(f"/api/admin/rooms/{room.code}/restart")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertFalse(room.tutorial)
        self.assertFalse(room.anonymous)

    def test_restart_without_the_didacticiel_turns_the_gate_off(self) -> None:
        # §8: « L'admin peut relancer une partie avec les mêmes joueurs, avec ou sans
        # didacticiel. » The admin screen posts `{"tutorial": false}` to this exact URL, and the
        # view used to never read the body at all — so the two buttons were one button, and a
        # teacher who chose "sans didacticiel" got a class sitting a QCM they had already sat.
        room = self._in_phase(Room.Phase.SUMMARY, tutorial=True)
        r = self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": False})
        self.assertEqual(r.status_code, 200)
        room.refresh_from_db()
        self.assertFalse(room.tutorial)
        # And it is not just a row that changed: `admin_start` reads this flag to choose between
        # the quiz and the writing, so the relaunch lands in the drafting round directly. Without
        # it the room would go `waiting -> quiz` and the class would have to answer eight
        # questions to reach the round they asked for.
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)

    def test_restart_with_the_didacticiel_turns_the_gate_back_on(self) -> None:
        # The other half, and the reason this is a flag and not a one-way latch: a room created
        # without the didacticiel can be relaunched with it, which is the same §8 sentence read
        # from the other button.
        room = self._in_phase(Room.Phase.SUMMARY, tutorial=False)
        self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": True})
        room.refresh_from_db()
        self.assertTrue(room.tutorial)
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.QUIZ)

    def test_restart_keeps_the_gate_when_the_body_says_nothing_about_it(self) -> None:
        # BOTH current values, so neither the default nor the polarity of the flag can hide here.
        # A client that posts nothing — the double-click guard, a curl, a build without the
        # buttons — must get today's behaviour rather than a silent reset of a setting nobody
        # chose: `tutorial=False` in the body would turn a gate on for a room that never had one.
        for flag in (True, False):
            with self.subTest(tutorial=flag):
                room = self._in_phase(Room.Phase.SUMMARY, tutorial=flag, code=f"KEP{int(flag)}")
                self._post(f"/api/admin/rooms/{room.code}/restart")
                room.refresh_from_db()
                self.assertEqual(room.tutorial, flag)

    def test_restart_ignores_a_tutorial_flag_that_is_not_a_boolean(self) -> None:
        # `isinstance(True, int)` is True and `bool("false")` is True, so a truthiness test
        # would let a client's typo invert the flag. A value that is not a JSON boolean is not a
        # choice, and leaves the room as it was — same guard as `rate`'s score.
        for i, sent in enumerate((1, 0, "false", "true", None, [])):
            with self.subTest(sent=sent):
                room = self._in_phase(Room.Phase.SUMMARY, tutorial=True, code=f"TYPE{i}")
                self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": sent})
                room.refresh_from_db()
                self.assertTrue(room.tutorial)

    def test_restart_does_not_take_anonymity_from_the_body(self) -> None:
        # §8 offers the didacticiel choice and only the didacticiel choice, and the Règles
        # transverses fix anonymity per game. So a hand-written `{"anonymous": false}` — the kind
        # of thing a stale client sends — must not be able to unmask a room the admin created
        # anonymous, with nothing in the payload to tell anybody it happened.
        room = self._in_phase(Room.Phase.SUMMARY, tutorial=False, anonymous=True)
        self._post(f"/api/admin/rooms/{room.code}/restart", {"anonymous": False, "tutorial": True})
        room.refresh_from_db()
        self.assertTrue(room.anonymous)
        self.assertTrue(room.tutorial)

    def test_restart_puts_the_quiz_cursor_and_its_window_back_to_nothing(self) -> None:
        # A RELAUNCHED GAME MUST NOT RESUME MID-QUESTION. The room now carries a cursor and a
        # deadline, and neither cascades off anything a restart deletes: the bank survives (it
        # belongs to the room), so a room parked on question 5 with a deadline that expired during
        # the teacher's coffee would come back on a question nobody was asked — and, since the
        # reveal is `now >= deadline`, on the KEY to it.
        room = self._quiz_room()
        for _ in range(3):
            self._next(room)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 3)
        self.assertIsNotNone(room.quiz_deadline)
        r = self._post(f"/api/admin/rooms/{room.code}/restart")
        self.assertEqual(r.status_code, 200)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index, room.quiz_deadline), ("waiting", 0, None))
        # And the relaunch really does start from the beginning rather than from wherever the last
        # game stopped: the second game asks question 0 first, with a fresh window on it.
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual((room.phase, room.quiz_index), (Room.Phase.QUIZ, 0))
        self.assertGreater(room.quiz_deadline, timezone.now())
        self.assertEqual(room_state(room.code)["quiz"]["index"], 0)

    def test_restart_without_the_didacticiel_leaves_no_question_to_resume(self) -> None:
        # The other relaunch button. §8: « Sans didacticiel, la relance va directement dans la
        # rédaction » — and "directly into the writing" has to mean no cursor either, or the room
        # carries a question it is not playing into a game where `quiz` is null in every snapshot.
        room = self._quiz_room()
        for _ in range(2):
            self._next(room)
        self._post(f"/api/admin/rooms/{room.code}/restart", {"tutorial": False})
        room.refresh_from_db()
        self.assertEqual((room.quiz_index, room.quiz_deadline), (0, None))
        self._post(f"/api/admin/rooms/{room.code}/start")
        room.refresh_from_db()
        self.assertEqual(room.phase, Room.Phase.PLAYING)
        self.assertIsNone(room.quiz_deadline)
        self.assertIsNone(room_state(room.code)["quiz"])

    def test_restart_erases_quiz_attempts_and_ratings(self) -> None:
        # Neither cascades off the rounds: a rating hangs off the answer, which the round delete
        # does take, but a quiz attempt hangs off the player and the question, and both survive.
        # Left behind, a returning player is told they already answered a question in a game that
        # no longer exists.
        room = self._in_phase(Room.Phase.SUMMARY, answers=2)
        authors = list(room.players.all())
        rater = Player.objects.create(room=room, name="zoe")
        question = QuizQuestion.objects.create(
            # `room=` because the bank belongs to the room: `QuizQuestion.room` is NOT NULL, and a
            # question that belonged to no room is the row this migration deletes rather than
            # invents a home for.
            room=room,
            order=0,
            prompt="q",
            choices=["a", "b", "c", "d"],
            correct=0,
            explanation="parce que",
        )
        QuizAttempt.objects.create(player=authors[0], question=question, choice=0, correct=True)
        Rating.objects.create(answer=authors[0].answers.first(), rater=rater, score=7)
        self.assertEqual((QuizAttempt.objects.count(), Rating.objects.count()), (1, 1))

        self._post(f"/api/admin/rooms/{room.code}/restart")
        self.assertEqual(QuizAttempt.objects.count(), 0)
        self.assertEqual(Rating.objects.count(), 0)
        # The bank is not the game's: it survives the reset, and so do the players.
        self.assertEqual(QuizQuestion.objects.count(), 1)
        self.assertEqual(room.players.count(), 3)

    def test_restart_erases_answers_and_keeps_tokens_usable(self) -> None:
        # The whole point of the feature: nobody rejoins. `ana`'s token from the first game is
        # what she submits with in the second, so this fails if the players were recreated.
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": False}).content)["code"]
        token = self._join(code, "ana")
        self._post(f"/api/admin/rooms/{code}/start")
        with patch("game.llm.answer", return_value="42"), patch(
            "game.llm.score", return_value=Verdict(7, "pas mal")
        ):
            self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": token, "prompt": "repete 42"}),
                content_type="application/json",
            )
        self.assertEqual(Answer.objects.count(), 1)

        self._post(f"/api/admin/rooms/{code}/restart")
        self.assertEqual(Answer.objects.count(), 0)  # cascade through the deleted rounds
        self.assertEqual(Player.objects.get(room_id=code, name="ana").token, token)

        # Second game, same player, same token, no rejoin.
        self.assertEqual(self._post(f"/api/admin/rooms/{code}/start").status_code, 200)
        with patch("game.llm.answer", return_value="42"), patch(
            "game.llm.score", return_value=Verdict(9, "nickel")
        ):
            r = self.c.post(
                f"/api/rooms/{code}/answer",
                data=json.dumps({"token": token, "prompt": "repete 42"}),
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content)["score"], 9)
        # Score is per-round, so the previous game's 7 is not carried over.
        self.assertEqual(Answer.objects.get().score, 9)
        self.assertEqual(Answer.objects.count(), 1)

    def test_second_restart_is_a_noop(self) -> None:
        # Double-clicking the admin button must not lock the admin out of their own room, so
        # this is 200 and not the 409 `admin_start` returns for its own second call.
        code = json.loads(self._post("/api/admin/rooms", {"tutorial": False}).content)["code"]
        self._join(code, "ana")
        self._post(f"/api/admin/rooms/{code}/start")
        url = f"/api/admin/rooms/{code}/restart"
        self.assertEqual(json.loads(self._post(url).content)["phase"], "waiting")
        again = self._post(url)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(json.loads(again.content), {"ok": True, "phase": "waiting"})
        self.assertEqual(Room.objects.get(code=code).players.count(), 1)

    def test_restart_on_unknown_room_is_404(self) -> None:
        self.assertEqual(self._post("/api/admin/rooms/NOPE/restart").status_code, 404)

    def _join(self, code: str, name: str) -> str:
        r = self.c.post(
            f"/api/rooms/{code}/join",
            data=json.dumps({"name": name}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        return json.loads(r.content)["token"]