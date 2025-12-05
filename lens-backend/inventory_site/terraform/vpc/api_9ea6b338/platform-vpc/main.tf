terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.default_region
}

resource "google_compute_network" "this" {
  count                   = var.create_network ? 1 : 0
  name                    = var.network_name
  auto_create_subnetworks = false
}

data "google_compute_network" "existing" {
  count   = var.create_network ? 0 : 1
  name    = var.network_name
  project = var.project_id
}

locals {
  network_self_link = var.create_network ? google_compute_network.this[0].self_link : data.google_compute_network.existing[0].self_link
  network_name      = var.create_network ? google_compute_network.this[0].name : data.google_compute_network.existing[0].name
}

resource "google_compute_subnetwork" "this" {
  for_each      = { for subnet in var.subnets : subnet.name => subnet }
  name          = each.value.name
  ip_cidr_range = each.value.cidr
  region        = each.value.region
  network       = local.network_self_link
  stack_type    = "IPV4_ONLY"
}

resource "google_compute_router" "router_platform_vpc_asia_south1_1" {
  name    = "router-platform-vpc-asia-south1-1"
  network = local.network_name
  region  = "asia-south1"
}

resource "google_compute_address" "nat_ip_platform_vpc_asia_south1_1" {
  name   = "nat-ip-platform-vpc-asia-south1-1"
  region = "asia-south1"
}

resource "google_compute_router_nat" "cloud_nat_platform_vpc_asia_south1_1" {
  name                               = "cloud-nat-platform-vpc-asia-south1-1"
  router                             = google_compute_router.router_platform_vpc_asia_south1_1.name
  region                             = "asia-south1"
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.nat_ip_platform_vpc_asia_south1_1.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  dynamic "subnetwork" {
    for_each = [for s in var.subnets : s if !s.is_public && s.region == "asia-south1"]
    content {
      name                    = google_compute_subnetwork.this[subnetwork.value.name].self_link
      source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
    }
  }
}
