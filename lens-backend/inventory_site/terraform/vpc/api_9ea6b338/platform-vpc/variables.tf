variable "project_id" {
  description = "Target GCP project ID"
  type        = string
}

variable "network_name" {
  description = "Name of the VPC network to create"
  type        = string
}

variable "default_region" {
  description = "Default GCP region for regional resources"
  type        = string
}

variable "create_network" {
  description = "Set to false when the VPC network already exists"
  type        = bool
  default     = true
}

variable "subnets" {
  description = "Subnets to create in the VPC"
  type = list(object({
    name      = string
    region    = string
    cidr      = string
    is_public = bool
  }))
}
