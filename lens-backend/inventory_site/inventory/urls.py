from django.urls import path

from inventory import views

urlpatterns = [
    path("", views.inventory_request_view, name="inventory_form"),
    path("api/tasks/run/", views.run_task_api, name="run_task_api"),
    path("api/tasks/run-stream/", views.run_task_stream, name="run_task_stream"),
    path("api/aws/vpcs/", views.aws_vpcs_api, name="aws_vpcs_api"),
    path("api/aws/subnets/", views.aws_subnets_api, name="aws_subnets_api"),
    path("api/aws/ecs/clusters/", views.aws_ecs_clusters_api, name="aws_ecs_clusters_api"),
    path("api/aws/ecs/services/", views.aws_ecs_services_api, name="aws_ecs_services_api"),
    path("api/aws/ecr-repos/", views.aws_ecr_repos_api, name="aws_ecr_repos_api"),
    path("api/gcp/projects/", views.gcp_projects_api, name="gcp_projects_api"),
    path("api/gcp/networks/", views.gcp_networks_api, name="gcp_networks_api"),
    path("api/gcp/network/", views.gcp_network_detail_api, name="gcp_network_detail_api"),
]
