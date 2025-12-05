from django.urls import path

from inventory import views

urlpatterns = [
    path("", views.inventory_request_view, name="inventory_form"),
    path("api/tasks/run/", views.run_task_api, name="run_task_api"),
    path("api/aws/vpcs/", views.aws_vpcs_api, name="aws_vpcs_api"),
    path("api/aws/subnets/", views.aws_subnets_api, name="aws_subnets_api"),
    path("api/gcp/projects/", views.gcp_projects_api, name="gcp_projects_api"),
    path("api/gcp/networks/", views.gcp_networks_api, name="gcp_networks_api"),
    path("api/gcp/network/", views.gcp_network_detail_api, name="gcp_network_detail_api"),
]
