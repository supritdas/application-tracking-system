from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .forms import AdminUserCreationForm
from .models import Application, Interview, Job, Resume, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = AdminUserCreationForm  # the stock form assumes a username field
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "role", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name", "role", "phone", "company_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "role", "password1", "password2")}),
    )


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("title", "company_name", "recruiter", "status", "created_at")
    list_filter = ("status", "job_type", "is_remote")
    search_fields = ("title", "company_name")


@admin.register(Resume)
class ResumeAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "owner", "parse_status", "uploaded_at")
    list_filter = ("parse_status",)


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("candidate", "job", "status", "match_score", "ai_status", "applied_at")
    list_filter = ("status", "ai_status")


admin.site.register(Interview)
