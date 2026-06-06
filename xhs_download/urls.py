from django.urls import path

from . import views

urlpatterns = [
    path("", views.search_view, name="search"),
    path("results/", views.search_results_view, name="results"),
    path("proxy-image/", views.proxy_image_view, name="proxy_image"),
    path("download-images/", views.download_images_api, name="download_images_api"),
    path("download-tasks/", views.create_download_task_api, name="download_task_create"),
    path("download-tasks/<str:task_id>/", views.download_task_status_api, name="download_task_status"),
    path("download-tasks/<str:task_id>/file/", views.download_task_file_api, name="download_task_file"),
]
