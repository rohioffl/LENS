project_id     = "ccd-poc-project"
network_name   = "platform-vpc"
default_region = "asia-south1"
create_network = true
subnets        = [
  {
    name   = "platform-private-2"
    region = "asia-south1"
    cidr   = "10.21.120.0/23"
    is_public = false
  },
  {
    name   = "platform-private-1"
    region = "asia-south1"
    cidr   = "10.21.110.0/24"
    is_public = false
  },
  {
    name   = "platform-public-1"
    region = "asia-south1"
    cidr   = "10.21.10.0/24"
    is_public = true
  },
  {
    name   = "platform-public-3"
    region = "asia-south1"
    cidr   = "10.21.30.0/24"
    is_public = true
  },
  {
    name   = "platform-private-3"
    region = "asia-south1"
    cidr   = "10.21.130.0/24"
    is_public = false
  },
  {
    name   = "platform-public-2"
    region = "asia-south1"
    cidr   = "10.21.20.0/24"
    is_public = true
  }
]
