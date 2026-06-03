from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Submission

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'image', 'created_at')