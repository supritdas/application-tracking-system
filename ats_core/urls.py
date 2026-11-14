from django.contrib import admin
from django.urls import include, path

# NOTE: media is intentionally NOT served. Resumes are only reachable through the
# permission-checked view in recruitment/views.py.
urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("recruitment.urls")),
]
