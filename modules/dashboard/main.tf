data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  module_tags = merge(var.tags, {
    Service = "dashboard"
  })

  bucket_name         = format("%s-dashboard-%s", var.project, data.aws_caller_identity.current.account_id)
  static_cache_policy = "no-cache, no-store, must-revalidate"
  favicon_ico_path    = "${dirname(var.index_html_path)}/favicon.ico"
}

resource "aws_s3_bucket" "dashboard" {
  # checkov:skip=CKV_AWS_18: Access logging deshabilitado en lab académico.
  # checkov:skip=CKV2_AWS_62: Sin notificaciones de eventos S3 (lab).
  # checkov:skip=CKV2_AWS_61: Sin lifecycle configuration (lab).
  # checkov:skip=CKV_AWS_144: Sin replicación S3 (lab).
  bucket        = local.bucket_name
  force_destroy = true

  tags = merge(local.module_tags, {
    Component = "s3-bucket"
    Name      = local.bucket_name
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id

  rule {
    apply_server_side_encryption_by_default {
      # checkov:skip=CKV2_AWS_67: AWS Academy no permite KMS CMK; AES256 cumple el requisito de cifrado at-rest.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id

  versioning_configuration {
    status = "Suspended"
  }
}

resource "aws_s3_bucket_website_configuration" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id

  index_document {
    suffix = "index.html"
  }
}

resource "aws_s3_bucket_public_access_block" "dashboard" {
  # checkov:skip=CKV2_AWS_6: Acceso público requerido para sitio web estático.
  bucket = aws_s3_bucket.dashboard.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadGetObject"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.dashboard.arn}/*"
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.dashboard]
}

resource "aws_s3_object" "index_html" {
  bucket        = aws_s3_bucket.dashboard.id
  key           = "index.html"
  source        = var.index_html_path
  source_hash   = filemd5(var.index_html_path)
  content_type  = "text/html"
  cache_control = local.static_cache_policy

  tags = merge(local.module_tags, {
    Component = "s3-object"
  })
}

resource "aws_s3_object" "app_js" {
  bucket        = aws_s3_bucket.dashboard.id
  key           = "app.js"
  source        = var.app_js_path
  source_hash   = filemd5(var.app_js_path)
  content_type  = "application/javascript"
  cache_control = local.static_cache_policy

  tags = merge(local.module_tags, {
    Component = "s3-object"
  })
}

resource "aws_s3_object" "favicon_ico" {
  bucket        = aws_s3_bucket.dashboard.id
  key           = "favicon.ico"
  source        = local.favicon_ico_path
  source_hash   = filemd5(local.favicon_ico_path)
  content_type  = "image/x-icon"
  cache_control = local.static_cache_policy

  tags = merge(local.module_tags, {
    Component = "s3-object"
  })
}

resource "aws_s3_object" "config_js" {
  bucket = aws_s3_bucket.dashboard.id
  key    = "config.js"
  content = templatefile(var.config_js_template_path, {
    api_endpoint               = var.api_endpoint
    cognito_user_pool_id       = var.cognito_user_pool_id
    cognito_client_id          = var.cognito_client_id
    cognito_domain_url         = var.cognito_domain_url
    cognito_hosted_ui_base_url = var.cognito_hosted_ui_base_url
    cognito_issuer             = var.cognito_issuer
    cognito_redirect_uri       = var.cognito_redirect_uri
    cognito_logout_uri         = var.cognito_logout_uri
  })
  content_type  = "application/javascript"
  cache_control = local.static_cache_policy

  tags = merge(local.module_tags, {
    Component = "s3-object"
  })
}
