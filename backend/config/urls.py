from django.urls import path

from game import views

urlpatterns = [
    path("api/admin/login", views.admin_login),
    path("api/admin/rooms", views.admin_create_room),
    path("api/admin/rooms/<str:code>/start", views.admin_start),
    path("api/admin/rooms/<str:code>/next", views.admin_next),
    path("api/admin/rooms/<str:code>/restart", views.admin_restart),
    path("api/rooms/<str:code>/join", views.join),
    path("api/rooms/<str:code>/events", views.events),
    path("api/rooms/<str:code>/answer", views.answer),
    # One pattern and not a `<int:answer_id>`: the rating endpoint's whole security property is
    # that the client never names what it is rating. See `views.rate`.
    path("api/rooms/<str:code>/rate", views.rate),
    # GET reads the bank, POST answers one question: two methods on ONE pattern, because the
    # resolver returns the first pattern that matches the path and never looks at the method.
    path("api/rooms/<str:code>/quiz", views.quiz),
]
