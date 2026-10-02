from django.urls import path

from game import views

urlpatterns = [
    path("api/admin/login", views.admin_login),
    # GET lists the rooms, POST creates one, and both live on this single pattern: the resolver
    # returns the first pattern that matches the path and never looks at the method, so a second
    # URL for the listing would either be unreachable or would take the creation with it. See
    # `views.admin_rooms`.
    path("api/admin/rooms", views.admin_rooms),
    path("api/admin/rooms/<str:code>/start", views.admin_start),
    path("api/admin/rooms/<str:code>/next", views.admin_next),
    path("api/admin/rooms/<str:code>/restart", views.admin_restart),
    path("api/rooms/<str:code>/join", views.join),
    path("api/rooms/<str:code>/events", views.events),
    path("api/rooms/<str:code>/answer", views.answer),
    # One pattern and not a `<int:answer_id>`: the rating endpoint's whole security property is
    # that the client never names what it is rating. See `views.rate`.
    path("api/rooms/<str:code>/rate", views.rate),
    # The ready vote, like `rate`: the client names its player and never the step.
    path("api/rooms/<str:code>/ready", views.ready),
    # GET reads the bank, POST answers one question: two methods on ONE pattern, because the
    # resolver returns the first pattern that matches the path and never looks at the method.
    path("api/rooms/<str:code>/quiz", views.quiz),
]
