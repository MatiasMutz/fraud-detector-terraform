data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  module_tags = merge(var.tags, {
    Service = "auth"
  })

  user_pool_name = format("%s-auth", var.project)
  domain_prefix  = format("itba-fraud-auth-%s", data.aws_caller_identity.current.account_id)

  google_oauth_enabled = var.google_oauth_client_id != "" && nonsensitive(var.google_oauth_client_secret) != ""

  supported_identity_providers = local.google_oauth_enabled ? ["COGNITO", "Google"] : ["COGNITO"]

  domain_url         = format("https://%s.auth.%s.amazoncognito.com", local.domain_prefix, data.aws_region.current.name)
  hosted_ui_base_url = format("%s/oauth2/authorize", local.domain_url)
  issuer             = format("https://cognito-idp.%s.amazonaws.com/%s", data.aws_region.current.name, aws_cognito_user_pool.this.id)
}

resource "aws_cognito_user_pool" "this" {
  # checkov:skip=CKV_AWS_130: MFA opcional fuera del alcance del lab académico.
  name                     = local.user_pool_name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"
  deletion_protection      = "INACTIVE"
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
  }

  tags = merge(local.module_tags, {
    Component = "cognito-user-pool"
    Name      = local.user_pool_name
  })
}

resource "aws_cognito_identity_provider" "google" {
  for_each = local.google_oauth_enabled ? { google = true } : {}

  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    authorize_scopes = "openid email profile"
    client_id        = var.google_oauth_client_id
    client_secret    = var.google_oauth_client_secret
  }

  attribute_mapping = {
    email       = "email"
    given_name  = "given_name"
    family_name = "family_name"
    picture     = "picture"
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "this" {
  name                                 = format("%s-auth-client", var.project)
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = local.supported_identity_providers
  prevent_user_existence_errors        = "ENABLED"

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  depends_on = [aws_cognito_identity_provider.google]
}
