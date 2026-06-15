locals {
  project         = "itba-tp-fraud"
  vpc_cidr        = "10.0.0.0/16"
  onprem_vpc_cidr = "192.168.0.0/16"

  common_tags = {
    Project   = local.project
    ManagedBy = "terraform"
  }

  service_tags = {
    api = merge(local.common_tags, {
      Service = "api"
    })
    data_store = merge(local.common_tags, {
      Service = "data-store"
    })
    results_writer = merge(local.common_tags, {
      Service = "results-writer"
    })
  }

  # Primeras dos AZs de la región (orden estable, conocido en plan) para
  # evitar count/for_each que dependan de random_shuffle.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  dashboard_app_url       = format("https://%s-dashboard-%s.s3.%s.amazonaws.com/index.html", local.project, data.aws_caller_identity.current.account_id, data.aws_region.current.name)
}
