module "network" {
  source = "./modules/network"

  project                          = local.project
  vpc_cidr                         = local.vpc_cidr
  azs                              = local.azs
  additional_endpoint_client_cidrs = var.enable_onprem_sim ? [local.onprem_vpc_cidr] : []
  tags                             = local.common_tags
}

# La cola autoriza LabRole; el acceso privado desde on-prem queda acotado por
# VPN Site-to-Site, DNS privado hacia el VPCE de SQS y el SG del endpoint.
module "queue" {
  source = "./modules/queue"

  project         = local.project
  principal_arn   = data.aws_iam_role.lab.arn
  onprem_vpc_cidr = var.enable_onprem_sim ? local.onprem_vpc_cidr : ""
  onprem_vpc_id   = var.enable_onprem_sim ? module.network.vpc_id : ""
  tags            = local.common_tags
}

module "data_store" {
  source = "./modules/data_store"

  project            = local.project
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.data_subnet_ids
  db_password        = random_password.db.result
  principal_arn      = data.aws_iam_role.lab.arn
  tags               = local.common_tags
}

module "compute" {
  source = "./modules/compute"

  project = local.project
  tags    = local.common_tags

  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.app_subnet_ids
  endpoint_security_group_id = module.network.endpoint_security_group_id

  task_role_arn      = data.aws_iam_role.lab.arn
  execution_role_arn = data.aws_iam_role.lab.arn

  image_uri     = var.image_uri
  task_cpu      = var.task_cpu
  task_memory   = var.task_memory
  desired_count = var.desired_count
  min_capacity  = var.min_capacity
  max_capacity  = var.max_capacity

  processor_concurrency = var.processor_concurrency
  processor_pollers     = var.processor_pollers

  queue_arn             = module.queue.queue_arn
  queue_url             = module.queue.queue_url
  queue_name            = module.queue.queue_name
  table_name            = module.data_store.table_name
  results_queue_url     = module.results_writer.queue_url
  fraud_alert_queue_url = module.notification.fraud_alert_queue_url
  audit_bucket_name     = module.data_store.audit_bucket_name
}

module "notification" {
  source = "./modules/notification"

  project                    = local.project
  principal_arn              = data.aws_iam_role.lab.arn
  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.app_subnet_ids
  endpoint_security_group_id = module.network.endpoint_security_group_id
  summarizer_package_file    = data.archive_file.notification_summarizer.output_path
  dashboard_url              = local.dashboard_app_url
  summary_interval_minutes   = var.fraud_alert_summary_interval_minutes
  tags                       = local.common_tags
}

module "results_writer" {
  source = "./modules/results_writer"

  project                     = local.project
  principal_arn               = data.aws_iam_role.lab.arn
  vpc_id                      = module.network.vpc_id
  private_subnet_ids          = module.network.app_subnet_ids
  endpoint_security_group_id  = module.network.endpoint_security_group_id
  db_host                     = module.data_store.proxy_endpoint
  db_port                     = module.data_store.db_port
  db_name                     = module.data_store.db_name
  db_username                 = module.data_store.db_username
  db_password                 = random_password.db.result
  package_file                = "${path.root}/app/results_writer/build/results-writer.zip"
  sqs_batch_size              = var.results_writer_batch_size
  sqs_batching_window_seconds = var.results_writer_batching_window_seconds
  tags                        = local.common_tags
}

module "auth" {
  source = "./modules/auth"

  project                    = local.project
  tags                       = local.common_tags
  callback_urls              = local.dashboard_callback_urls
  logout_urls                = local.dashboard_logout_urls
  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret
}

module "api" {
  source = "./modules/api"

  project                    = local.project
  principal_arn              = data.aws_iam_role.lab.arn
  vpc_id                     = module.network.vpc_id
  private_subnet_ids         = module.network.app_subnet_ids
  endpoint_security_group_id = module.network.endpoint_security_group_id
  psycopg2_layer_arn         = aws_lambda_layer_version.psycopg2.arn
  db_host                    = module.data_store.proxy_endpoint
  db_port                    = module.data_store.db_port
  db_name                    = module.data_store.db_name
  db_username                = module.data_store.db_username
  db_password                = random_password.db.result
  sns_topic_arn              = module.notification.topic_arn
  user_behavior_table_name   = module.data_store.table_name
  jwt_issuer                 = module.auth.issuer
  jwt_audience               = module.auth.client_id
  package_file               = data.archive_file.api_lambda.output_path
  timeout_seconds            = var.api_lambda_timeout_seconds
  tags                       = local.common_tags
}

module "dashboard" {
  source = "./modules/dashboard"

  project                    = local.project
  api_endpoint               = module.api.api_endpoint
  cognito_user_pool_id       = module.auth.user_pool_id
  cognito_client_id          = module.auth.client_id
  cognito_domain_url         = module.auth.domain_url
  cognito_hosted_ui_base_url = module.auth.hosted_ui_base_url
  cognito_issuer             = module.auth.issuer
  cognito_redirect_uri       = local.dashboard_app_url
  cognito_logout_uri         = local.dashboard_app_url
  index_html_path            = "${path.root}/app/dashboard/index.html"
  app_js_path                = "${path.root}/app/dashboard/app.js"
  config_js_template_path    = "${path.root}/app/dashboard/config.js.tpl"
  tags                       = local.common_tags
}

# Simulación de un sitio on-premise: una VPC aparte con un EC2 strongSwan
# que actúa de router IPsec, junto con el Customer Gateway y la conexión
# Site-to-Site VPN contra el VGW que provee el módulo network.
module "onprem_sim" {
  source = "./modules/onprem_sim"
  count  = var.enable_onprem_sim ? 1 : 0

  project                                = local.project
  azs                                    = local.azs
  vpn_gateway_id                         = module.network.vpn_gateway_id
  aws_vpc_cidr                           = module.network.vpc_cidr
  onprem_vpc_cidr                        = local.onprem_vpc_cidr
  sqs_vpce_eni_count                     = length(local.azs)
  sqs_vpc_endpoint_network_interface_ids = module.network.sqs_vpc_endpoint_network_interface_ids
  enable_traffic_producers               = var.enable_onprem_traffic_producers
  ingestion_queue_url                    = module.queue.queue_url
  tags                                   = local.common_tags
}
