from django.urls import path

from inventory import views
from inventory import views_chatbot

urlpatterns = [
    path("", views.inventory_request_view, name="inventory_form"),
    path("api/tasks/run/", views.run_task_api, name="run_task_api"),
    path("api/tasks/run-stream/", views.run_task_stream, name="run_task_stream"),
    path("api/aws/vpcs/", views.aws_vpcs_api, name="aws_vpcs_api"),
    path("api/aws/subnets/", views.aws_subnets_api, name="aws_subnets_api"),
    path("api/aws/ecs/clusters/", views.aws_ecs_clusters_api, name="aws_ecs_clusters_api"),
    path("api/aws/ecs/services/", views.aws_ecs_services_api, name="aws_ecs_services_api"),
    path("api/aws/eks/clusters/", views.aws_eks_clusters_api, name="aws_eks_clusters_api"),
    path("api/aws/eks/namespaces/", views.aws_eks_namespaces_api, name="aws_eks_namespaces_api"),
    path("api/aws/ecr-repos/", views.aws_ecr_repos_api, name="aws_ecr_repos_api"),
    path("api/aws/ec2/instances/", views.aws_ec2_instances_api, name="aws_ec2_instances_api"),
    path("api/aws/ec2/docker/", views.aws_instance_docker_containers_api, name="aws_instance_docker_containers_api"),
    path("api/gcp/compute/instances/", views.gcp_compute_instances_api, name="gcp_compute_instances_api"),
    path("api/gcp/compute/docker/", views.gcp_instance_docker_containers_api, name="gcp_instance_docker_containers_api"),
    path("api/box/metadata/", views.box_project_metadata_api, name="box_project_metadata_api"),
    path("api/box/aws/regions/", views.box_project_aws_regions_api, name="box_project_aws_regions_api"),
    path("api/box/aws/ec2-data/", views.box_project_aws_ec2_data_api, name="box_project_aws_ec2_data_api"),
    path("api/box/aws/rds-data/", views.box_project_aws_rds_data_api, name="box_project_aws_rds_data_api"),
    path("api/box/aws/availability-zones/", views.box_project_aws_availability_zones_api, name="box_project_aws_availability_zones_api"),
    path("api/box/aws/generate-key-pair/", views.box_project_aws_generate_key_pair_api, name="box_project_aws_generate_key_pair_api"),
    path("api/gcp/projects/", views.gcp_projects_api, name="gcp_projects_api"),
    path("api/gcp/networks/", views.gcp_networks_api, name="gcp_networks_api"),
    path("api/gcp/network/", views.gcp_network_detail_api, name="gcp_network_detail_api"),
    # Chatbot endpoints
    path("api/chat/send/", views_chatbot.chat_send_message, name="chat_send_message"),
    path("api/chat/send-stream/", views_chatbot.chat_send_message_stream, name="chat_send_message_stream"),
    path("api/chat/history/", views_chatbot.chat_get_history, name="chat_get_history"),
    path("api/chat/clear/", views_chatbot.chat_clear_history, name="chat_clear_history"),
]
