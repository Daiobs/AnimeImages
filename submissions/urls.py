from django.urls import path

from . import views

urlpatterns = [
    path('submit/', views.submit_post, name='submit_post'),
    path('list', views.submission_list, name='submission_list'),
    path('detail/<int:submission_id>/', views.submission_detail, name='submission_detail'),
]