# from django.urls import path
# from . import views
#
# urlpatterns = [
#     path('', views.search_view, name='search'),
#     path('results/', views.search_results_view, name='xhs_search_results'),
#     path('download_images/', views.download_images_api, name='download_images_api'),
from django.urls import path
from . import views

urlpatterns = [
    path("", views.search_view, name="search"),
    path("results/", views.search_results_view, name="results"),
    path("proxy-image/", views.proxy_image_view, name="proxy_image"),
    path("download-images/", views.download_images_api, name="download_images_api"),
]
# ]
