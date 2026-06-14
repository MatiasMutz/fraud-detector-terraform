# Proxy ↔ RDS

resource "aws_vpc_security_group_egress_rule" "proxy_to_rds" {
  security_group_id            = module.data_store.proxy_security_group_id
  description                  = "PostgreSQL desde proxy hacia RDS"
  referenced_security_group_id = module.data_store.rds_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.data_store, {
    Component = "security-group-rule"
  })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_proxy" {
  security_group_id            = module.data_store.rds_security_group_id
  description                  = "PostgreSQL desde RDS Proxy"
  referenced_security_group_id = module.data_store.proxy_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.data_store, {
    Component = "security-group-rule"
  })
}

# Lambda results-writer ↔ Proxy

resource "aws_vpc_security_group_egress_rule" "writer_lambda_to_proxy" {
  security_group_id            = module.results_writer.lambda_security_group_id
  description                  = "PostgreSQL desde Lambda results-writer hacia RDS Proxy"
  referenced_security_group_id = module.data_store.proxy_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.results_writer, {
    Component = "security-group-rule"
  })
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_writer_lambda" {
  security_group_id            = module.data_store.proxy_security_group_id
  description                  = "PostgreSQL desde Lambda results-writer"
  referenced_security_group_id = module.results_writer.lambda_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.results_writer, {
    Component = "security-group-rule"
  })
}

# Lambda api ↔ Proxy

resource "aws_vpc_security_group_egress_rule" "api_lambda_to_proxy" {
  security_group_id            = module.api.lambda_security_group_id
  description                  = "PostgreSQL desde Lambda API hacia RDS Proxy"
  referenced_security_group_id = module.data_store.proxy_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.api, {
    Component = "security-group-rule"
  })
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_api_lambda" {
  security_group_id            = module.data_store.proxy_security_group_id
  description                  = "PostgreSQL desde Lambda API"
  referenced_security_group_id = module.api.lambda_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432

  tags = merge(local.service_tags.api, {
    Component = "security-group-rule"
  })
}
