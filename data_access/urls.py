from django.urls import path

from .views import DataCatalogView, DataQueryView


urlpatterns = [
    path("catalog/", DataCatalogView.as_view(), name="data_access_catalog"),
    path("query/", DataQueryView.as_view(), name="data_access_query"),
]
