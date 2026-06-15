data "aws_region" "current" {}

locals {
  module_tags = merge(var.tags, {
    Service = "api"
  })

  function_name  = format("%s-api", var.project)
  log_group_name = format("/aws/lambda/%s-api", var.project)
  api_name       = format("%s-api", var.project)

  cors_preflight_route_keys = toset([
    "OPTIONS /{proxy+}",
    "OPTIONS /dashboard/me",
    "OPTIONS /dashboard/me/password",
    "OPTIONS /dashboard/invites",
    "OPTIONS /dashboard/invites/{id}",
    "OPTIONS /filters",
    "OPTIONS /health",
    "OPTIONS /stats",
    "OPTIONS /stats/timeseries",
    "OPTIONS /transactions",
    "OPTIONS /transactions/{id}",
    "OPTIONS /users",
    "OPTIONS /users/{id}",
    "OPTIONS /users/{id}/behavior",
  ])
}

resource "aws_cloudwatch_log_group" "api_lambda" {
  # checkov:skip=CKV_AWS_158: AWS Academy no permite KMS CMK; cifrado con clave AWS-owned.
  # checkov:skip=CKV_AWS_338: retención corta acorde al alcance académico.
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = merge(local.module_tags, {
    Component = "cloudwatch-log-group"
    Name      = local.log_group_name
  })
}

resource "aws_security_group" "api_lambda" {
  name        = format("%s-api-lambda-sg", var.project)
  description = "Lambda API; no ingress; egress to VPC endpoints (tcp/443) and RDS (tcp/5432). Cross-module SG rules are managed in the root composition."
  vpc_id      = var.vpc_id

  tags = merge(local.module_tags, {
    Component = "security-group"
    Name      = format("%s-api-lambda-sg", var.project)
  })
}

resource "aws_vpc_security_group_egress_rule" "api_to_endpoints" {
  security_group_id            = aws_security_group.api_lambda.id
  description                  = "HTTPS hacia los Interface VPC Endpoints (Logs, SNS)"
  referenced_security_group_id = var.endpoint_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

data "aws_prefix_list" "dynamodb" {
  name = format("com.amazonaws.%s.dynamodb", data.aws_region.current.name)
}

resource "aws_vpc_security_group_egress_rule" "api_to_dynamodb" {
  security_group_id = aws_security_group.api_lambda.id
  description       = "HTTPS hacia DynamoDB via Gateway VPC Endpoint (user behavior profiles)"
  prefix_list_id    = data.aws_prefix_list.dynamodb.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443

  tags = merge(local.module_tags, {
    Component = "security-group-rule"
  })
}

resource "aws_lambda_function" "api" {
  # checkov:skip=CKV_AWS_173: AWS Academy no expone alias/aws/lambda; env vars usan cifrado por defecto del servicio.
  # checkov:skip=CKV_AWS_272: Code signing no configurado en lab académico.
  # checkov:skip=CKV_AWS_50: X-Ray tracing deshabilitado en lab.
  # checkov:skip=CKV_AWS_116: Lambda síncrona; DLQ no aplica (API Gateway reintenta a nivel HTTP).
  function_name                  = local.function_name
  role                           = var.principal_arn
  runtime                        = "python3.12"
  handler                        = "handler.handler"
  timeout                        = var.timeout_seconds
  memory_size                    = 256
  layers                         = [var.psycopg2_layer_arn]
  reserved_concurrent_executions = 3 # AWS Academy account cap is 10 concurrent Lambdas total

  filename         = var.package_file
  source_code_hash = filebase64sha256(var.package_file)

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.api_lambda.id]
  }

  environment {
    variables = {
      DB_HOST                  = var.db_host
      DB_PORT                  = tostring(var.db_port)
      DB_NAME                  = var.db_name
      DB_USER                  = var.db_username
      DB_PASSWORD              = var.db_password
      SUMMARY_SNS_TOPIC_ARN    = var.sns_topic_arn
      USER_BEHAVIOR_TABLE_NAME = var.user_behavior_table_name
      AUTH_LOCAL_BYPASS        = "false"
    }
  }

  depends_on = [aws_cloudwatch_log_group.api_lambda]

  tags = merge(local.module_tags, {
    Component = "lambda"
    Name      = local.function_name
  })
}

resource "aws_apigatewayv2_api" "main" {
  # checkov:skip=CKV2_AWS_29: WAF no disponible en lab académico.
  name          = local.api_name
  protocol_type = "HTTP"
  description   = "Dashboard API para consulta de resultados de fraude."

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization", "X-Cognito-Access-Token", "X-Trace-Id"]
    max_age       = 300
  }

  tags = merge(local.module_tags, {
    Component = "api-gateway"
    Name      = local.api_name
  })
}

resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.main.id
  authorizer_type  = "JWT"
  name             = format("%s-jwt-authorizer", var.project)
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [var.jwt_audience]
    issuer   = var.jwt_issuer
  }
}

resource "aws_cloudwatch_log_group" "api_gw" {
  # checkov:skip=CKV_AWS_158: AWS Academy no permite KMS CMK.
  # checkov:skip=CKV_AWS_338: retención corta acorde al alcance académico.
  name              = format("/aws/apigateway/%s-api", var.project)
  retention_in_days = var.log_retention_days

  tags = merge(local.module_tags, {
    Component = "cloudwatch-log-group"
    Name      = format("/aws/apigateway/%s-api", var.project)
  })
}

resource "aws_apigatewayv2_stage" "default" {
  # checkov:skip=CKV_AWS_76: Access logging con ARN de CloudWatch Logs requiere permisos extra de IAM no disponibles en Academy.
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  tags = merge(local.module_tags, {
    Component = "api-gateway-stage"
    Name      = format("%s-api-default-stage", var.project)
  })
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_transactions" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /transactions"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_stats" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /stats"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_health" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /health"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_stats_timeseries" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /stats/timeseries"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_filters" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /filters"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_transaction_by_id" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /transactions/{id}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_users" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /users"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_user_by_id" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /users/{id}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_user_behavior" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /users/{id}/behavior"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_dashboard_me" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /dashboard/me"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "put_dashboard_password" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "PUT /dashboard/me/password"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "get_dashboard_invites" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /dashboard/invites"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "post_dashboard_invites" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "POST /dashboard/invites"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "delete_dashboard_invite" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "DELETE /dashboard/invites/{id}"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_apigatewayv2_route" "cors_preflight" {
  for_each = local.cors_preflight_route_keys

  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

moved {
  from = aws_apigatewayv2_route.cors_preflight
  to   = aws_apigatewayv2_route.cors_preflight["OPTIONS /{proxy+}"]
}

resource "aws_apigatewayv2_route" "default" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
  authorization_type = "JWT"
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
